import hashlib
import json
import io
import subprocess
import tarfile
from pathlib import Path
import unittest

BASE = Path(__file__).resolve().parents[1]/'docs/research/evidence/2026-09-27-m0-pid'
ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT/'docs/research/evidence/2026-09-27-pid-review'

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

class RemainingPidEvidenceTests(unittest.TestCase):
    def test_review_integrity_and_sources(self):
        for bundle in REVIEW.iterdir():
            hashes=read(bundle/'bundle-sha256.json')
            self.assertEqual(set(hashes),{p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file() and p.name!='bundle-sha256.json'})
            for path,digest in hashes.items():
                self.assertEqual(hashlib.sha256((bundle/path).read_bytes()).hexdigest(),digest,path)
        for bundle in [REVIEW/'interactive',REVIEW/'noninteractive']:
            commit=read(bundle/'m0-policy.json')['source_commit']
            archive=tarfile.open(fileobj=io.BytesIO(subprocess.check_output(['git','-c','core.autocrlf=true','archive',commit],cwd=ROOT)))
            for name in ['m0-source.json','source-manifest.json']:
                for path,digest in read(bundle/name).items():
                    self.assertEqual(hashlib.sha256(archive.extractfile(path).read()).hexdigest(),digest,path)
        for bundle in [BASE,REVIEW/'pilot']:
            for path,item in read(bundle/'source-recovery.json').items():
                if 'git_commit' in item:
                    data=subprocess.check_output(['git','show',item['git_commit']+':'+item['git_path']],cwd=ROOT)
                    data=b''.join(line.replace(b'\n',b'\r\n') if i in item['crlf_line_numbers'] else line for i,line in enumerate(data.splitlines(keepends=True)))
                else:
                    data=(bundle/item['path']).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(),item['sha256'],path)

    def test_management_workers_and_shell_modes(self):
        for mode,count in [('interactive',8),('noninteractive',1)]:
            bundle=REVIEW/mode
            rows=read(bundle/'results.json')
            self.assertEqual(len(rows),count)
            for row in rows:
                name=f"cpu{row['cpu']}-n{row['guest']}-{row['kind']}"
                self.assertNotIn('error',row)
                self.assertEqual(row['host'],2*row['guest']+128)
                self.assertEqual(row['probe_exit'],0)
                self.assertEqual(row['npm_exit'],0)
                self.assertEqual(row['tmux_after'],0)
                self.assertTrue(row['inspection']['State']['Running'])
                self.assertFalse(row['inspection']['State']['OOMKilled'])
                self.assertEqual(row['recovery']['exit'],0)
                self.assertEqual(row['recovery']['stdout'],'RECOVERY_EXEC_OK')
                self.assertEqual(row['shell_exit_while_full'],None if mode=='interactive' else 254)
                self.assertEqual(row['shell_exit'],0 if mode=='interactive' else 254)
                self.assertEqual(len(row['full_exec']),4)
                self.assertEqual(len(row['management_exec']),4)
                for result in row['full_exec']:
                    self.assertEqual(result['exit'],128)
                    self.assertIn('try again',result['stdout'])
                for result in row['management_exec']:
                    self.assertEqual(result['exit'],0)
                    self.assertEqual(result['stdout'],'ADMIN_FORK_OK')
                    self.assertIn('1001:1001',result['command'])
                    self.assertEqual(result['command'][-1],'/bin/true && printf ADMIN_FORK_OK')
                events={e['event']:e for e in map(json.loads,(bundle/(name+'-probe.txt')).read_text().splitlines())}
                self.assertEqual(events['at_limit']['tasks'],row['guest'])
                self.assertIsNotNone(events['at_limit']['failure'])
                self.assertEqual(events['soft_within_hard']['restored'],[row['guest']]*2)
                for key in ['uid_root','uid_other']:
                    self.assertEqual(events['negative']['results'][key]['errno'],1)
                before=json.loads(events['workers_before']['value'])
                after=json.loads(events['workers_full']['value'])
                self.assertEqual(len(before),8)
                self.assertEqual(len(after),8)
                self.assertTrue(all(a>b for a,b in zip(after,before)))
                samples=read(bundle/(name+'-samples.json'))
                self.assertFalse(any('error' in s for s in samples))
                self.assertEqual({s['phase'] for s in samples},{'workload','pressure','full','recovery'})
                self.assertEqual({s['pids.events'] for s in samples},{'max 0'})
                self.assertLess(max(int(s['pids.current']) for s in samples),row['host'])

    def test_reported_peaks_exclude_recovery(self):
        expected={(2,64,'threads'):(45,53),(2,64,'fork'):(133,143),
                  (2,128,'threads'):(46,50),(2,128,'fork'):(263,271),
                  (4,64,'threads'):(50,62),(4,64,'fork'):(137,145),
                  (4,128,'threads'):(51,55),(4,128,'fork'):(263,274)}
        for (cpu,n,kind),peaks in expected.items():
            samples=read(REVIEW/'interactive'/f'cpu{cpu}-n{n}-{kind}-samples.json')
            self.assertEqual(tuple(max(int(s['pids.current']) for s in samples if s['phase']==phase) for phase in ['pressure','full']),peaks)

    def test_integrity(self):
        hashes = read(BASE/'bundle-sha256.json')
        self.assertEqual(set(hashes),{p.relative_to(BASE).as_posix() for p in BASE.rglob('*') if p.is_file() and p.name!='bundle-sha256.json'})
        for path,digest in hashes.items():
            self.assertEqual(hashlib.sha256((BASE/path).read_bytes()).hexdigest(),digest,path)

    def test_matrix_rejection_recovery_and_explicit_full_shell_failure(self):
        rows = read(BASE/'results.json')
        self.assertEqual({(r['cpu'],r['guest'],r['kind']) for r in rows},
                         {(c,n,k) for c in [2,4] for n in [64,128] for k in ['threads','fork']})
        for row in rows:
            with self.subTest(cpu=row['cpu'],guest=row['guest'],kind=row['kind']):
                self.assertNotIn('error',row)
                name=f"cpu{row['cpu']}-n{row['guest']}-{row['kind']}"
                events=[json.loads(line) for line in (BASE/(name+'-probe.txt')).read_text().splitlines()]
                limit=next(e for e in events if e['event']=='at_limit')
                self.assertEqual(limit['tasks'],row['guest'])
                self.assertIsNotNone(limit['failure'])
                negative=next(e for e in events if e['event']=='negative')['results']
                self.assertTrue(all(isinstance(v,dict) for v in negative.values()))
                self.assertEqual(negative['uid_root']['errno'],1)
                self.assertEqual(negative['uid_other']['errno'],1)
                before=next(e['value'] for e in events if e['event']=='heartbeat_before')
                after=next(e['value'] for e in events if e['event']=='heartbeat_full')
                self.assertGreater(int(after),int(before))
                self.assertEqual(row['npm_exit'],0)
                self.assertEqual(row['probe_exit'],0)
                self.assertEqual(row['tmux_after'],0)
                self.assertEqual(row['recovery']['exit'],0)
                self.assertEqual(row['recovery']['stdout'],'RECOVERY_EXEC_OK')
                self.assertEqual(len(row['full_exec']),4)
                for result in row['full_exec']:
                    self.assertNotEqual(result['exit'],0)
                    self.assertIn('try again',result['stdout']+result['stderr'])
                shell=(BASE/(name+'-shell.txt')).read_text()
                self.assertIn('SHELL_FORK_BEFORE=0',shell)
                self.assertIn('SHELL_FORK_AFTER=0',shell)
                self.assertIn('fork: Resource temporarily unavailable',shell)
                self.assertNotIn('SHELL_FORK_FULL=0',shell)
                samples=read(BASE/(name+'-samples.json'))
                self.assertEqual({s['pids.events'] for s in samples},{'max 0'})
