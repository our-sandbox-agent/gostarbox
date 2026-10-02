"""Tests for the gated E05/E10 runner preparation (scripts/e5e10_runner.py).

Coverage per #75 (無 key 的腳本/fixture 可先做) and #70 (PID taxonomy):
  - config gating: each required pin/limit missing or invalid -> precise
    refusal; an unconfigured run is impossible (plan() raises, CLI exits 2);
    key_source must be an existing local 0600 file OUTSIDE the repo
  - plan invariants: NOT-RUN everywhere (no dry-run-success look), E05
    marker content+sha256+session/cwd record, E10 identity-change assertion,
    volumes kept, docker-start-is-not-recreation, no-key-no-start,
    explicit-session resume, session_missing taxonomy + ADR quote, #70
    launch fixture, evidence/cleanup/cost discipline
  - classification matrix: the four #70 outcomes, precedence, and
    tmux-alive-is-never-success
  - session gate matrix
  - mutation guards (CONTRIBUTING mutate-and-fail idiom): each guard breaks
    one safeguard and asserts the invariant flips on the mutant while it
    holds on the real code.
"""
import hashlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location('e5e10_runner', ROOT / 'scripts/e5e10_runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

RunnerConfig = runner.RunnerConfig
ConfigError = runner.ConfigError
MARKER_CONTENT = runner.MARKER_CONTENT
OUTCOMES = runner.OUTCOMES

VALID = dict(
    repo_commit='02dc635051bb005d65e31f0a7e1f81e747498bd3',
    image_digest='sha256:' + 'a' * 64,
    claude_version='1.0.33', node_version='v22.9.0',
    tmux_version='3.3a', runsc_version='runsc release-20250929.0 systrap',
    model_id='claude-sonnet-4-5',
    timeout_s=600, max_turns=10, max_tokens=100000,
    budget_usd=5.0, stop_method='budget-stop: docker stop -t 1 + rm, label-scoped',
)

ALIVE = dict(eagain_seen=False, shell_pid_alive=True, agent_pid_alive=True,
             container_alive=True, sentry_events=[])
RECORD = {'session_id': 'e05-session-0001', 'cwd': '/workspace/repo'}


def complete_config(key_path):
    return RunnerConfig(key_source=str(key_path), **VALID)


class ConfigGatingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.key = Path(self._tmp.name) / 'anthropic_key'
        self.key.write_text('sk-ant-api03-EXAMPLEONLYNOTREAL0123456789\n')
        self.key.chmod(0o600)

    def tearDown(self):
        self._tmp.cleanup()

    def test_complete_config_validates_clean(self):
        self.assertEqual(complete_config(self.key).validate(), [])

    def test_every_required_field_missing_is_refused_by_name(self):
        for field in VALID:
            with self.subTest(field=field):
                config = complete_config(self.key)
                setattr(config, field, None)
                problems = config.validate()
                self.assertTrue(problems, f'{field} missing must refuse')
                self.assertTrue(any(p.startswith(field + ':') for p in problems),
                                problems)

    def test_bad_commit_and_digest_refused(self):
        config = complete_config(self.key)
        config.repo_commit = 'HEAD'
        config.image_digest = 'node:22'
        problems = config.validate()
        self.assertTrue(any('40-hex' in p for p in problems))
        self.assertTrue(any('sha256:<64-hex>' in p for p in problems))

    def test_zero_limits_refused(self):
        for field in ('timeout_s', 'max_turns', 'max_tokens', 'budget_usd'):
            with self.subTest(field=field):
                config = complete_config(self.key)
                setattr(config, field, 0)
                self.assertTrue(any(p.startswith(field + ':')
                                    for p in config.validate()))

    def test_missing_key_file_refused(self):
        config = complete_config(self.key)
        config.key_source = str(Path(self._tmp.name) / 'nope')
        self.assertTrue(any('does not exist' in p for p in config.validate()))

    def test_world_readable_key_file_refused(self):
        self.key.chmod(0o644)
        problems = complete_config(self.key).validate()
        self.assertTrue(any('chmod 600' in p for p in problems))

    def test_key_file_inside_repo_refused(self):
        config = complete_config(self.key)
        config.key_source = str(ROOT / 'local' / 'key')
        self.assertTrue(any('OUTSIDE this repository' in p
                            for p in config.validate()))

    def test_relative_key_path_refused(self):
        config = complete_config(self.key)
        config.key_source = 'secrets/key'
        self.assertTrue(any('absolute' in p for p in config.validate()))

    def test_plan_refuses_unconfigured_config(self):
        config = complete_config(self.key)
        config.budget_usd = None
        with self.assertRaises(ConfigError) as raised:
            runner.plan(config)
        self.assertIn('budget_usd', str(raised.exception))


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.key = Path(self._tmp.name) / 'anthropic_key'
        self.key.write_text('sk-ant-EXAMPLEONLY\n')
        self.key.chmod(0o600)

    def tearDown(self):
        self._tmp.cleanup()

    def cli(self, *extra):
        argv = [sys.executable, str(ROOT / 'scripts/e5e10_runner.py')] + list(extra)
        return subprocess.run(argv, capture_output=True, text=True)

    def flags(self, drop=None):
        args = [part for k, v in VALID.items()
                for part in (f'--{k.replace("_", "-")}', str(v)) if k != drop]
        return args + ['--key-source', str(self.key)]

    def test_check_config_ok_exits_zero(self):
        result = self.cli('--check-config', *self.flags())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_config_missing_budget_exits_2_with_precise_message(self):
        result = self.cli('--check-config', *self.flags(drop='budget_usd'))
        self.assertEqual(result.returncode, 2)
        self.assertIn('budget_usd', result.stderr)

    def test_plan_invalid_config_exits_2(self):
        result = self.cli('--plan', '--image-digest', 'latest')
        self.assertEqual(result.returncode, 2)
        self.assertIn('image_digest', result.stderr)

    def test_plan_valid_config_emits_not_run_marker(self):
        result = self.cli('--plan', *self.flags())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('NOT-RUN', result.stdout)
        self.assertIn('NOTHING WAS EXECUTED', result.stdout)
        self.assertNotIn('"status": "pass"', result.stdout)

    def test_run_is_refused(self):
        result = self.cli('--run')
        self.assertEqual(result.returncode, 2)
        self.assertIn('#75', result.stderr)
        self.assertIn('blocked', result.stderr.lower())

    def test_no_mode_chosen_is_an_error(self):
        result = self.cli()
        self.assertEqual(result.returncode, 2)


class PlanContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        key = Path(cls._tmp.name) / 'anthropic_key'
        key.write_text('sk-ant-EXAMPLEONLY\n')
        key.chmod(0o600)
        cls.document = runner.plan(complete_config(key))
        cls.text = runner.render_plan(cls.document)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_top_level_status_is_not_run(self):
        self.assertEqual(self.document['status'], 'not_run')
        for section in ('e05', 'e10', 'launch_fixture_70'):
            self.assertEqual(self.document[section]['status'], 'not_run')

    def test_text_banner_cannot_be_mistaken_for_a_pass(self):
        self.assertIn('EXPERIMENT PLAN — STATUS: NOT-RUN', self.text)
        self.assertIn('STILL NOT-RUN', self.text)
        self.assertIn('"status": "not_run"', self.text)

    def test_blocked_reason_quotes_the_issue(self):
        blocked = self.document['blocked']
        self.assertIn('準備無 key 的腳本/fixture 可先做', blocked['reason'])
        for item in ('key', 'model', 'budget', 'host'):
            self.assertTrue(any(item in m for m in blocked['missing']), item)

    def test_pins_and_limits_are_echoed(self):
        self.assertEqual(self.document['pins']['repo_commit'], VALID['repo_commit'])
        self.assertEqual(self.document['pins']['image_digest'], VALID['image_digest'])
        self.assertEqual(self.document['pins']['model_id'], VALID['model_id'])
        self.assertEqual(self.document['limits']['budget_usd'], VALID['budget_usd'])
        self.assertEqual(self.document['limits']['stop_method'], VALID['stop_method'])
        self.assertIn('never continue unbounded', self.document['limits']['rule'])

    def test_e05_marker_content_hash_session_and_cwd(self):
        e05 = self.document['e05']
        marker = e05['marker']
        self.assertEqual(marker['content'], MARKER_CONTENT)
        self.assertEqual(marker['sha256'],
                         hashlib.sha256(MARKER_CONTENT.encode()).hexdigest())
        self.assertTrue(marker['path'].startswith('/workspace/'))
        self.assertIn('exit code', ' '.join(e05['verify']))
        self.assertTrue({'session_id', 'cwd'} <= set(e05['record']))
        self.assertEqual(e05['limits']['max_turns'], VALID['max_turns'])

    def test_e10_asserts_identity_change_and_keeps_both_volumes(self):
        steps = {s['step']: s['action'] for s in self.document['e10']['steps']}
        self.assertIn('must all differ', steps['identity_changed'])
        kept = steps['volumes_kept']
        for volume, dest in runner.APPROVED_VOLUMES:
            self.assertIn(volume, kept)
            self.assertIn(dest, kept)
        self.assertIn('NEVER deleted during recreation', kept)

    def test_e10_says_docker_start_is_not_recreation(self):
        recreate = next(s['action'] for s in self.document['e10']['steps']
                        if s['step'] == 'recreate')
        self.assertIn('docker start', recreate)
        self.assertIn('NOT recreation', recreate)

    def test_e10_no_key_no_start_and_explicit_session_resume(self):
        steps = {s['step']: s['action'] for s in self.document['e10']['steps']}
        self.assertIn('Claude must NOT start', steps['no_key_no_start'])
        self.assertIn('credentials_required', steps['no_key_no_start'])
        self.assertIn('EXPLICIT session ID', steps['reinject_and_resume'])
        self.assertIn('prior conversation CONTENT', steps['conversation_verified'])

    def test_e10_session_missing_taxonomy_and_adr_quote(self):
        e10 = self.document['e10']
        taxonomy = e10['session_gate']
        self.assertIn('session_missing', taxonomy['key_session_missing'])
        self.assertIn('conversation continuation', taxonomy['key_session_missing'])
        self.assertIn('Keeping files does not prove a Claude process resumes',
                      e10['adr_quote'])
        not_claimed = ' '.join(e10['not_claimed'])
        self.assertIn('docker start', not_claimed)
        self.assertIn('session-ID reconnection', not_claimed)

    def test_launch_fixture_70_records_tree_pids_and_exit_codes(self):
        fixture = self.document['launch_fixture_70']
        recorded = ' '.join(fixture['record'])
        self.assertIn('parent-child PIDs', recorded)
        self.assertIn('exit codes', recorded)
        self.assertEqual(tuple(fixture['four_way']), OUTCOMES)
        self.assertIn('tmux still alive is NEVER task success',
                      fixture['invariant'])
        self.assertIn('#70', fixture['shared_base'])

    def test_evidence_cleanup_and_cost_discipline(self):
        evidence = self.document['evidence']
        self.assertIn('gvisor-publish-evidence.py', evidence['publish'])
        self.assertIn('OUTSIDE the repository', evidence['raw_location'])
        self.assertTrue(any('leaks == []' in c for c in evidence['checks']))
        cleanup = ' '.join(self.document['cleanup']['steps'])
        self.assertIn('label', self.document['cleanup']['scope'])
        self.assertIn('intent-before-mutation', self.document['cleanup']['scope'])
        cost = self.document['cost_record']
        for field in ('cost_usd_known', 'cost_usd', 'provider_usage_raw_ref',
                      'stop_method_triggered', 'cumulative_spend_usd'):
            self.assertIn(field, ' '.join(cost['fields']))


class ClassificationTests(unittest.TestCase):
    def classify(self, **overrides):
        return runner.classify_pid_outcome({**ALIVE, **overrides})

    def test_four_outcome_matrix(self):
        cases = [
            ({'eagain_seen': True}, 'new_command_eagain_rejection'),
            ({'shell_pid_alive': False}, 'shell_exit'),
            ({'agent_pid_alive': False}, 'agent_exit'),
            ({'container_alive': False}, 'container_or_sentry_death'),
            ({'sentry_events': ['sentry panic']}, 'container_or_sentry_death'),
            ({}, 'no_failure_observed'),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(self.classify(**overrides)['outcome'], expected)

    def test_precedence_most_severe_wins(self):
        dead_everything = self.classify(
            eagain_seen=True, shell_pid_alive=False, agent_pid_alive=False,
            container_alive=False)
        self.assertEqual(dead_everything['outcome'], 'container_or_sentry_death')
        self.assertEqual(self.classify(shell_pid_alive=False,
                                       agent_pid_alive=False)['outcome'],
                         'agent_exit')

    def test_task_success_is_false_for_every_outcome(self):
        for overrides in ({'eagain_seen': True}, {'shell_pid_alive': False},
                          {'agent_pid_alive': False}, {'container_alive': False},
                          {}):
            with self.subTest(overrides=overrides):
                self.assertFalse(self.classify(**overrides)['task_success'])

    def test_tmux_alive_is_not_task_success(self):
        # #70: 不能用tmux仍活代表Claude任務成功 — tmux aliveness is not even
        # an input, and never flips task_success.
        result = self.classify(agent_pid_alive=False, tmux_pid_alive=True)
        self.assertEqual(result['outcome'], 'agent_exit')
        self.assertFalse(result['task_success'])
        quiet = self.classify(tmux_pid_alive=True)
        self.assertEqual(quiet['outcome'], 'no_failure_observed')
        self.assertFalse(quiet['task_success'])
        self.assertIn('tmux', quiet['note'])

    def test_missing_observation_key_raises(self):
        with self.assertRaises(ValueError):
            runner.classify_pid_outcome({'eagain_seen': False})


class SessionGateTests(unittest.TestCase):
    def test_no_key_is_credentials_required_even_with_a_record(self):
        result = runner.session_gate(False, dict(RECORD))
        self.assertFalse(result['allowed'])
        self.assertEqual(result['code'], 'credentials_required')
        self.assertIsNone(result['session_id'])

    def test_key_and_record_resumes_by_explicit_session_id(self):
        result = runner.session_gate(True, dict(RECORD))
        self.assertTrue(result['allowed'])
        self.assertEqual(result['code'], 'resume_by_explicit_session_id')
        self.assertEqual(result['session_id'], RECORD['session_id'])
        self.assertEqual(result['cwd'], RECORD['cwd'])

    def test_key_with_missing_or_unusable_record_is_session_missing(self):
        for record in (None, {}, {'session_id': 'x'}, {'cwd': '/w'},
                       {'session_id': '  ', 'cwd': '/w'}):
            with self.subTest(record=record):
                result = runner.session_gate(True, record)
                self.assertFalse(result['allowed'])
                self.assertEqual(result['code'], 'session_missing')
                self.assertIsNone(result['session_id'])

    def test_session_missing_note_forbids_masquerade(self):
        note = runner.session_gate(True, None)['note']
        self.assertIn('never', note)
        self.assertIn('conversation continuation', note)


# ---------------------------------------------------------- mutation guards
# Each mutant breaks one safeguard at a deliberate point; each guard proves
# the invariant flips on the mutant and holds on the real code
# (CONTRIBUTING mutate-and-fail idiom).


class NoBudgetCheckConfig(RunnerConfig):
    def validate(self):
        return [p for p in super().validate() if not p.startswith('budget_usd')]


class NoStopMethodCheckConfig(RunnerConfig):
    def validate(self):
        return [p for p in super().validate() if not p.startswith('stop_method')]


def tmux_success_classifier(observations):
    # the bug (#70): tmux still alive is treated as the task succeeding
    result = runner.classify_pid_outcome(observations)
    if observations.get('tmux_pid_alive'):
        result['task_success'] = True
    return result


def masquerading_gate(has_key, session_record):
    # the bug (#75): missing record silently becomes a NEW session that is
    # reported as if the conversation continued
    if not has_key:
        return runner.session_gate(False, session_record)
    return {'allowed': True, 'code': 'new_session_started', 'session_id': None,
            'note': 'started fresh'}


def shallow_classifier(observations):
    # the bug: shell exit is checked before container death, misattributing
    # a container/Sentry death that also killed the shell
    if not observations['shell_pid_alive']:
        return {'outcome': 'shell_exit', 'task_success': False, 'note': 'bug'}
    return runner.classify_pid_outcome(observations)


class MutationGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.key = Path(cls._tmp.name) / 'anthropic_key'
        cls.key.write_text('sk-ant-EXAMPLEONLY\n')
        cls.key.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def config(self, cls_=RunnerConfig):
        return cls_(key_source=str(self.key), **VALID)

    def test_guard_budget_requirement_is_load_bearing(self):
        real = self.config()
        real.budget_usd = None
        broken = self.config(NoBudgetCheckConfig)
        broken.budget_usd = None
        self.assertTrue(any(p.startswith('budget_usd') for p in real.validate()))
        self.assertFalse(any(p.startswith('budget_usd')
                             for p in broken.validate()))  # mutant lets it through

    def test_guard_stop_method_requirement_is_load_bearing(self):
        real = self.config()
        real.stop_method = None
        broken = self.config(NoStopMethodCheckConfig)
        broken.stop_method = None
        self.assertTrue(any(p.startswith('stop_method')
                            for p in real.validate()))
        self.assertFalse(any(p.startswith('stop_method')
                             for p in broken.validate()))

    def test_guard_plan_refuses_what_unvalidated_build_would_allow(self):
        unconfigured = self.config()
        unconfigured.model_id = None
        with self.assertRaises(ConfigError):
            runner.plan(unconfigured)
        # the mutant (plan without the validation gate) emits a document
        self.assertIsInstance(runner.build_plan(unconfigured), dict)

    def test_guard_tmux_as_success_is_detected(self):
        observations = {**ALIVE, 'agent_pid_alive': False, 'tmux_pid_alive': True}
        self.assertFalse(runner.classify_pid_outcome(observations)['task_success'])
        self.assertTrue(tmux_success_classifier(observations)['task_success'])

    def test_guard_silent_new_session_is_detected(self):
        real = runner.session_gate(True, None)
        self.assertFalse(real['allowed'])
        self.assertEqual(real['code'], 'session_missing')
        mutant = masquerading_gate(True, None)
        self.assertTrue(mutant['allowed'])  # masquerade flips the invariant
        self.assertNotEqual(mutant['code'], 'session_missing')

    def test_guard_classification_precedence_is_load_bearing(self):
        both_dead = {**ALIVE, 'shell_pid_alive': False, 'container_alive': False}
        self.assertEqual(runner.classify_pid_outcome(both_dead)['outcome'],
                         'container_or_sentry_death')
        self.assertEqual(shallow_classifier(both_dead)['outcome'],
                         'shell_exit')  # mutant misattributes the death


if __name__ == '__main__':
    unittest.main()
