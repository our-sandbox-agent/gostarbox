#!/usr/bin/env python3
"""#67 allocation limits vs host OOM; private raw output, bounded dedicated VM."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import select
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[1]

def main():
    spec = importlib.util.spec_from_file_location('matrix', ROOT/'scripts/gvisor-no-key-matrix.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--modes',nargs='+',choices=['as','data','none','as-multi'],default=['as','data','none'])
    args = parser.parse_args()
    if not args.image.startswith('sha256:'):
        parser.error('immutable image required')
    m = mod.Matrix(args)
    start = mod.now()
    rows = []
    volumes = []
    m.write('memory-policy.json', {'issue':67,'modes':args.modes, 'guest_limit_mib':96,
        'host_memory_mib':256,'max_touched_mib':512,'cpus':2,'pids':512,'model_calls':0,
        'scope':'rlimit applies to the allocator child, not aggregate sandbox memory',
        'sampling':'1 ms snapshots plus POLLPRI memory.events notifications'})
    files = [Path(__file__),ROOT/'diagnostics/gvisor/probes/memory-session.py']
    m.write('memory-source.json',{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
    try:
        for command in [['uname','-a'],['docker','version'],['runsc','--version'],['cat','/etc/docker/daemon.json']]:
            m.command(command)
        for runtime in ['runsc','runc']:
            for mode in args.modes:
                m.label = f'{runtime}-{mode}'
                name = 'spike-'+m.run_id[:8]+'-'+m.label
                entry = {'name':name,'id':None,'label':m.run_id}
                m.resources.append(entry)
                m.write('manifest.json',m.resources)
                mounts = []
                for suffix,destination in [('workspace','/workspace'),('home','/home/node')]:
                    volume = name+'-'+suffix
                    m.dock('volume','create','--label','sandbox.spike='+m.run_id,volume)
                    volumes.append(volume)
                    mounts += ['--mount',f'type=volume,src={volume},dst={destination}']
                    m.write('volume-manifest.json',volumes)
                command = ['create','--name',name,'--label','sandbox.spike='+m.run_id,
                    '--runtime',runtime,'--pull=never','--user','1000:1000','--cpus','2',
                    '--memory','256m','--memory-swap','256m','--pids-limit','512',
                    '--cap-drop','ALL','--security-opt','no-new-privileges'] + mounts + [args.image,'sleep','infinity']
                cid = m.dock(*command).stdout.strip()
                entry['id'] = cid
                m.write('manifest.json',m.resources)
                m.dock('start',cid)
                cg = m.cgroup(cid)
                m.dock('cp',str(files[1]),cid+':/workspace/memory-session.py')
                m.execute(cid,'tmux','new-session','-d','-s','work',
                    'python3 -c "import time; from pathlib import Path; [(Path(\'/workspace/heartbeat\').write_text(str(i)), time.sleep(.1)) for i in range(1200)]"')
                time.sleep(.5)
                row = {'runtime':runtime,'mode':mode,'started':mod.now()}
                if mode == 'as-multi':
                    smoke = m.execute(cid,'python3','-c',
                        'import os,resource; resource.setrlimit(resource.RLIMIT_CORE,(0,0)); resource.setrlimit(resource.RLIMIT_AS,(100663296,100663296)); os.execvp("node",["node","-e","console.log(123)"])',
                        check=False,timeout=10)
                    row['node_as_smoke'] = {'exit':smoke.returncode,'stdout':smoke.stdout,'stderr':smoke.stderr}
                samples = []
                event_samples = []
                stop = threading.Event()
                def sampling():
                    while not stop.is_set():
                        samples.append(m.stats(cg))
                        time.sleep(.001)
                def events():
                    try:
                        with (cg/'memory.events').open() as f:
                            poller = select.poll()
                            poller.register(f,select.POLLPRI|select.POLLERR)
                            while not stop.is_set():
                                f.seek(0)
                                event_samples.append({'at':time.monotonic(),'events':f.read()})
                                poller.poll(10)
                    except OSError as error:
                        event_samples.append({'error':str(error)})
                samplers = [threading.Thread(target=sampling),threading.Thread(target=events)]
                for thread in samplers:
                    thread.start()
                shell = None
                try:
                    row['heartbeat_before'] = m.execute(cid,'cat','/workspace/heartbeat',check=False).stdout
                    with (m.out/(m.label+'-shell.txt')).open('w') as shell_out:
                        shell_command = m.docker+['exec','-i',cid,'sh']
                        m.log({'shell_command':shell_command})
                        shell = subprocess.Popen(shell_command,stdin=subprocess.PIPE,stdout=shell_out,stderr=subprocess.STDOUT,text=True,env=m.env)
                        shell.stdin.write('printf "SHELL_BEFORE\\n"\n')
                        shell.stdin.flush()
                        probe = m.execute(cid,'python3','/workspace/memory-session.py',mode,check=False,timeout=30)
                        row['probe'] = {'exit':probe.returncode,'stdout':probe.stdout,'stderr':probe.stderr}
                        time.sleep(.2)
                        row['inspect'] = m.inspect(cid,'.')
                        try:
                            shell.stdin.write('printf "SHELL_AFTER\\n"\nexit\n')
                            shell.stdin.flush()
                        except BrokenPipeError:
                            pass
                        shell.wait(timeout=5)
                    row['heartbeat_after'] = m.execute(cid,'cat','/workspace/heartbeat',check=False).stdout
                    r = m.execute(cid,'sh','-c','printf RECOVERY_EXEC_OK',check=False)
                    row['recovery'] = {'exit':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
                    row['tmux_after'] = m.execute(cid,'tmux','has-session','-t','work',check=False).returncode
                    if not row['inspect']['State']['Running']:
                        m.dock('start',cid)
                        row['restart_tmux'] = m.execute(cid,'tmux','has-session','-t','work',check=False).returncode
                        r = m.execute(cid,'sh','-c','printf RESTART_EXEC_OK',check=False)
                        row['restart'] = {'exit':r.returncode,'stdout':r.stdout}
                    row['files_after'] = m.execute(cid,'cat','/workspace/saved.txt','/home/node/saved.txt',check=False).stdout
                except Exception as error:
                    row['error'] = repr(error)
                finally:
                    if shell is not None and shell.poll() is None:
                        shell.kill()
                        shell.wait()
                    stop.set()
                    for thread in samplers:
                        thread.join()
                    m.write(m.label+'-samples.json',samples)
                    m.write(m.label+'-memory-events.json',event_samples)
                    row['finished'] = mod.now()
                    rows.append(row)
                    m.write('results.json',rows)
                    m.dock('stop','-t','1',cid,check=False)
        m.label = 'final'
        for command in [['journalctl','-k','--since',start,'--no-pager'],['journalctl','-u','docker','--since',start,'--no-pager'],['uname','-a'],['docker','version'],['runsc','--version']]:
            m.command(command,check=False)
    finally:
        m.cleanup()
        for volume in volumes:
            info = json.loads(m.dock('volume','inspect',volume).stdout)[0]
            if info['Labels'].get('sandbox.spike') != m.run_id:
                raise RuntimeError('volume label mismatch')
            m.dock('volume','rm',volume)
        m.write('volumes-cleaned.json',{'removed':volumes})

if __name__ == '__main__':
    main()
