#!/usr/bin/env python3
"""#70 bounded CPU/guest-limit/fork/thread matrix. Raw output stays private."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[1]

def main():
    spec = importlib.util.spec_from_file_location('matrix', ROOT / 'scripts/gvisor-no-key-matrix.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not args.image.startswith('sha256:'):
        parser.error('immutable local image required')
    m = mod.Matrix(args)
    started = mod.now()
    rows = []
    m.write('m0-policy.json', {'issue': 70, 'cpus': [2,4], 'guest_nproc': [64,128],
        'host_formula': '2 * guest_nproc * (cpus + 1) + 64', 'memory': '2g',
        'max_allocations': 272, 'concurrent_exec': 4, 'model_calls': 0})
    files = [Path(__file__), ROOT/'diagnostics/gvisor/probes/task-pressure.py', ROOT/'diagnostics/gvisor/probes/node-workload.cjs', ROOT/'package.json', ROOT/'package-lock.json']
    m.write('m0-source.json', {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
    try:
        for command in [['uname','-a'], ['docker','version'], ['runsc','--version'], ['cat','/etc/docker/daemon.json']]:
            m.command(command)
        for cpu in [2,4]:
            for guest in [64,128]:
                for kind in ['threads','fork']:
                    m.label = f'cpu{cpu}-n{guest}-{kind}'
                    host = 2 * guest * (cpu + 1) + 64
                    cid = m.create(m.label, cpu=str(cpu), memory='2g', pids=str(host), guest_pids=guest)
                    cg = m.cgroup(cid)
                    for path in files[1:]:
                        m.dock('cp', str(path), cid + ':/workspace/' + path.name)
                    m.execute(cid, 'tmux', 'new-session', '-d', '-s', 'work', 'node /workspace/node-workload.cjs')
                    time.sleep(1)
                    samples = []
                    stop = threading.Event()
                    def sampling():
                        while not stop.is_set():
                            samples.append(m.stats(cg))
                            time.sleep(.005)
                    sampler = threading.Thread(target=sampling)
                    sampler.start()
                    probe = shell = None
                    row = {'cpu': cpu, 'guest': guest, 'host': host, 'kind': kind, 'started': mod.now()}
                    try:
                        install = m.execute(cid, 'sh', '-c', 'cd /workspace && npm ci --ignore-scripts', timeout=60, check=False)
                        row['npm_exit'] = install.returncode
                        row['baseline'] = m.execute(cid, 'python3', '-c', 'import os,json; print(json.dumps({p:len(os.listdir("/proc/"+p+"/task")) for p in os.listdir("/proc") if p.isdigit()}))', check=False).stdout
                        shell_path = m.out / (m.label + '-shell.txt')
                        probe_path = m.out / (m.label + '-probe.txt')
                        with shell_path.open('w') as shell_out, probe_path.open('w') as probe_out:
                            shell_cmd = m.docker + ['exec','-i',cid,'bash','--noprofile','--norc','-i']
                            shell = subprocess.Popen(shell_cmd, stdin=subprocess.PIPE, stdout=shell_out, stderr=subprocess.STDOUT, text=True, env=m.env)
                            shell.stdin.write('/bin/true; printf "SHELL_FORK_BEFORE=%s\\n" "$?"\n')
                            shell.stdin.flush()
                            time.sleep(.3)
                            command = m.docker + ['exec','-i',cid,'python3','/workspace/task-pressure.py',kind]
                            m.log({'shell_command': shell_cmd, 'probe_command': command})
                            probe = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=probe_out, stderr=subprocess.STDOUT, text=True, env=m.env)
                            deadline = time.monotonic()+30
                            while probe.poll() is None and '"event": "at_limit"' not in probe_path.read_text():
                                if mod.available_mib() < 2048 or time.monotonic() > deadline:
                                    raise RuntimeError('pressure guard')
                                time.sleep(.02)
                            # The already-running probe samples heartbeat: Docker cp itself
                            # may require runtime work and fail at the guest task limit.
                            try:
                                shell.stdin.write('/bin/true; printf "SHELL_FORK_FULL=%s\\n" "$?"\nprintf "SHELL_BUILTIN_FULL\\n"\n')
                                shell.stdin.flush()
                            except BrokenPipeError:
                                pass
                            def concurrent_exec(index):
                                r = subprocess.run(m.docker+['exec',cid,'sh','-c','printf FULL_EXEC_OK'], env=m.env, capture_output=True,text=True,timeout=20)
                                return {'index':index,'exit':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
                            with ThreadPoolExecutor(max_workers=4) as pool:
                                row['full_exec'] = list(pool.map(concurrent_exec, range(4)))
                            time.sleep(16)  # bash's bounded fork retry must finish while pressure is held.
                            if probe.poll() is None:
                                probe.stdin.write('release\n')
                                probe.stdin.flush()
                            probe.wait(timeout=10)
                            row['probe_exit'] = probe.returncode
                            try:
                                shell.stdin.write('/bin/true; printf "SHELL_FORK_AFTER=%s\\n" "$?"\nexit\n')
                                shell.stdin.flush()
                            except BrokenPipeError:
                                pass
                            shell.wait(timeout=10)
                        recovery = m.execute(cid,'sh','-c','printf RECOVERY_EXEC_OK',check=False)
                        row['recovery'] = {'exit':recovery.returncode,'stdout':recovery.stdout,'stderr':recovery.stderr}
                        row['tmux_after'] = m.execute(cid,'tmux','has-session','-t','work',check=False).returncode
                        row['inspection'] = m.inspect(cid,'.')
                    except Exception as error:
                        row['error'] = repr(error)
                    finally:
                        for client in [probe,shell]:
                            if client is not None and client.poll() is None:
                                client.kill()
                                client.wait()
                        stop.set()
                        sampler.join()
                        m.write(m.label+'-samples.json',samples)
                        row['finished'] = mod.now()
                        rows.append(row)
                        m.write('results.json',rows)
                        m.dock('stop','-t','1',cid,check=False)
        m.label = 'final'
        for command in [['journalctl','-k','--since',started,'--no-pager'], ['journalctl','-u','docker','--since',started,'--no-pager'], ['uname','-a'], ['docker','version'], ['runsc','--version']]:
            m.command(command,check=False)
    finally:
        m.cleanup()

if __name__ == '__main__':
    main()
