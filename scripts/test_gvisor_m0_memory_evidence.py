import hashlib
import importlib.util
import json
import subprocess
import io
import tarfile
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'docs/research/evidence/2026-09-27-m0-memory'

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

class MemorySessionEvidenceTests(unittest.TestCase):
    def test_bundle_integrity(self):
        for bundle in BASE.iterdir():
            hashes=read(bundle/'bundle-sha256.json')
            self.assertEqual(set(hashes),{p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file() and p.name!='bundle-sha256.json'})
            for path,digest in hashes.items():
                self.assertEqual(hashlib.sha256((bundle/path).read_bytes()).hexdigest(),digest,path)

    def test_survival_oom_attribution_and_durable_markers(self):
        count=0
        for bundle in BASE.iterdir():
            for row in read(bundle/'results.json'):
                count+=1
                with self.subTest(runtime=row['runtime'],mode=row['mode']):
                    self.assertNotIn('error',row)
                    name=row['runtime']+'-'+row['mode']
                    state=row['inspect']['State']
                    terminated=row['runtime']=='runsc' and row['mode']!='as'
                    oom=row['mode'] in ['none','as-multi'] or (row['runtime']=='runsc' and row['mode']=='data')
                    self.assertEqual(state['Running'],not terminated)
                    self.assertEqual(state['OOMKilled'],oom)
                    counters=[]
                    for sample in read(bundle/(name+'-memory-events.json')):
                        values=dict(line.split() for line in sample.get('events','').splitlines())
                        if 'oom_kill' in values:
                            counters.append(int(values['oom_kill']))
                    self.assertEqual(max(counters),int(oom))
                    self.assertEqual(row['files_after'],'FSYNCED_BEFORE_PRESSURE\n'*2)
                    if terminated:
                        self.assertEqual(state['ExitCode'],137)
                        self.assertNotEqual(row['recovery']['exit'],0)
                        self.assertNotEqual(row['tmux_after'],0)
                        self.assertEqual(row['restart'],{'exit':0,'stdout':'RESTART_EXEC_OK'})
                        self.assertNotEqual(row['restart_tmux'],0)
                    else:
                        self.assertEqual(row['probe']['exit'],0)
                        self.assertIn('child_reaped',row['probe']['stdout'])
                        self.assertGreater(int(row['heartbeat_after']),int(row['heartbeat_before']))
                        self.assertIn('SHELL_AFTER',(bundle/(name+'-shell.txt')).read_text())
                        self.assertEqual(row['recovery']['exit'],0)
                        self.assertEqual(row['recovery']['stdout'],'RECOVERY_EXEC_OK')
                        self.assertEqual(row['tmux_after'],0)
                    if row['mode']=='as-multi':
                        self.assertNotEqual(row['node_as_smoke']['exit'],0)
        self.assertEqual(count,16)

    def test_measured_sources_are_reconstructable(self):
        bundle=BASE/'review72'
        commit=read(bundle/'memory-policy.json')['source_commit']
        archive=tarfile.open(fileobj=io.BytesIO(subprocess.check_output(['git','-c','core.autocrlf=true','archive',commit],cwd=ROOT)))
        for name in ['memory-source.json','source-manifest.json']:
            for path,digest in read(bundle/name).items():
                data=archive.extractfile(path).read()
                self.assertEqual(hashlib.sha256(data).hexdigest(),digest,path)
        for name in ['published-67-base','published-67-multi']:
            bundle=BASE/name
            for path,item in read(bundle/'source-recovery.json').items():
                if 'git_commit' in item:
                    data=subprocess.check_output(['git','show',item['git_commit']+':'+item['git_path']],cwd=ROOT)
                    data=b''.join(line.replace(b'\n',b'\r\n') if i in item['crlf_line_numbers'] else line for i,line in enumerate(data.splitlines(keepends=True)))
                else:
                    data=(bundle/item['path']).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(),item['sha256'],path)

    def test_restart_counters_belong_to_a_new_instance(self):
        bundle=BASE/'review72'
        for row in read(bundle/'results.json'):
            if 'restarted_cgroup' not in row:
                continue
            old=row['cgroup_instance']
            new=row['restarted_cgroup']
            self.assertNotEqual(old['cgroup_inode'],new['inode'])
            self.assertNotEqual(old['started_at'],new['started_at'])
            self.assertIn('oom_kill 0',new['stats']['memory.events'])
            samples=read(bundle/(row['runtime']+'-'+row['mode']+'-samples.json'))
            for sample in samples:
                self.assertEqual(sample['instance'],old)

    def test_scope_guard_never_becomes_runtime_go(self):
        spec=importlib.util.spec_from_file_location('report',ROOT/'scripts/gvisor-report.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        root=BASE/'published-67-base'
        result,code=module.check(read(root/'matrix-report.json'),root)
        self.assertEqual(code,2)
        self.assertFalse(result['runtime_go'])
