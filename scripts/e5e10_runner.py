#!/usr/bin/env python3
"""#75/#70 gated E05/E10 experiment runner PREPARATION (stdlib only).

Real execution is BLOCKED per #75 line 4: 「準備無 key 的腳本/fixture 可先做，
付費執行待配置及授權」 — no authorized host, no owner-provided local API key
location, no explicit model ID, no budget/stop authorization. This slice
delivers ONLY the gated planner and fixtures, testable without
docker/network/key:

  RunnerConfig.validate() — an unconfigured run is impossible: every pin
      (repo commit, image digest, claude/node/tmux/runsc versions, model
      ID), every per-run limit (timeout/turns/tokens), the budget AND the
      stop method must be set, and key_source must be an existing LOCAL
      private file (never argv value, never env-persistent, never inside
      this repo). Missing anything -> precise refusal (CLI exit 2).
  plan(config) — the experiment plan document for E05 (one small marker
      task), E10 (cold recreation + explicit session resume), the #70
      tmux→shell→Claude launch fixture, evidence/cleanup/cost discipline.
      Always marked NOT-RUN; there is deliberately NO dry-run-success
      output and NO --run (nothing is executed, nothing is mocked).
  classify_pid_outcome(observations) — #70's four-way pressure taxonomy
      as a pure function; tmux still alive is never task success.
  session_gate(has_key, session_record) — no key -> credentials_required
      (Claude must not start); key + session record -> resume by EXPLICIT
      session ID; key + record missing -> explicit session_missing
      failure, never a silent new session masquerading as continuation.

Spec: docs/contracts/e5e10-runner.md. Tests: scripts/test_e5e10_runner.py.
"""
import argparse
import hashlib
import json
import re
from dataclasses import dataclass, fields
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

FIXTURE_REPO_URL = 'https://github.com/our-sandbox-agent/sandbox-console.git'
MARKER_PATH = '/workspace/e05-marker.txt'
MARKER_CONTENT = 'gostarbox-e05-marker-v1\n'
APPROVED_VOLUMES = (('workspace', '/workspace'), ('home', '/home/node'))
CLEANUP_LABEL_KEY = 'sandbox.spike'

ADR_SESSION_QUOTE = ('Claude session-ID reconnection is unverified until paid '
                     'E05/E10. Keeping files does not prove a Claude process '
                     'resumes. New tmux/session must be created after restart.')
DOCKER_START_NOT_RECREATION = (
    'docker start of the SAME container is NOT recreation: it retains the '
    'container identity and the writable layer (ADR memory-session-recovery: '
    'the earlier experiment "tested only docker start on the same container, '
    'which also retains its writable layer; it did not validate recreation"). '
    'E10 must docker rm this run\'s container and create a NEW instance from '
    'the same digest.')
BLOCKED_QUOTE = ('#75: Blocked：尚缺擁有者提供專用 API key 的本機配置位置、明確模型 ID、'
                 '費用預算與停止門檻。聊天提過 US$5 只是建議，未視為付款或模型呼叫授權。'
                 '準備無 key 的腳本/fixture 可先做，付費執行待配置及授權。')

NOT_RUN = 'not_run'


class ConfigError(ValueError):
    """A run/plan was attempted with an unconfigured or invalid config."""


# --------------------------------------------------------------- config gate

@dataclass
class RunnerConfig:
    """Everything #75 requires pinned before a paid E05/E10 run.

    key_source is a PATH to a local private file (mode 0600, outside this
    repo). The key VALUE never enters argv, environment, logs, the plan or
    any persisted output; this slice never even reads the file content.
    """
    repo_commit: str = None
    image_digest: str = None
    claude_version: str = None
    node_version: str = None
    tmux_version: str = None
    runsc_version: str = None
    model_id: str = None
    timeout_s: int = None
    max_turns: int = None
    max_tokens: int = None
    budget_usd: float = None
    key_source: str = None
    stop_method: str = None

    def validate(self):
        """Return a precise problem list; [] means the run is fully configured.

        Refusal is the safe default: anything missing, unpinned, or unsafe
        (key file readable by others, key file inside the repo) is named
        explicitly so the operator can fix exactly that.
        """
        problems = []

        def need_text(name, value):
            if not isinstance(value, str) or not value.strip():
                problems.append(f'{name}: required and must be a non-empty string')

        need_text('repo_commit', self.repo_commit)
        if isinstance(self.repo_commit, str) and self.repo_commit.strip() and \
                not re.fullmatch(r'[0-9a-f]{40}', self.repo_commit):
            problems.append('repo_commit: must be a full 40-hex git commit')
        need_text('image_digest', self.image_digest)
        if isinstance(self.image_digest, str) and self.image_digest.strip() and \
                not re.fullmatch(r'sha256:[0-9a-f]{64}', self.image_digest):
            problems.append('image_digest: must be an immutable sha256:<64-hex> digest')
        for name in ('claude_version', 'node_version', 'tmux_version',
                     'runsc_version', 'model_id'):
            need_text(name, getattr(self, name))
        for name in ('timeout_s', 'max_turns', 'max_tokens'):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                problems.append(f'{name}: required and must be a positive integer')
        if not isinstance(self.budget_usd, (int, float)) or isinstance(self.budget_usd, bool) \
                or self.budget_usd <= 0:
            problems.append('budget_usd: required and must be a positive number '
                            '(費用預算；no unbounded runs)')
        need_text('stop_method', self.stop_method)
        problems.extend(self._key_source_problems())
        return problems

    def _key_source_problems(self):
        name = 'key_source'
        if not isinstance(self.key_source, str) or not self.key_source.strip():
            return [f'{name}: required — path to the LOCAL private key file '
                    '(never a value in argv/env)']
        problems = []
        path = Path(self.key_source)
        if not path.is_absolute():
            problems.append(f'{name}: must be an absolute path')
        try:
            resolved = path.resolve()
        except OSError as error:
            return [f'{name}: unresolvable ({error})']
        if resolved == ROOT or ROOT in resolved.parents:
            problems.append(f'{name}: must live OUTSIDE this repository')
        if not path.is_file():
            problems.append(f'{name}: local file does not exist: {path}')
        else:
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                problems.append(f'{name}: file must not be group/world readable '
                                f'(chmod 600); current mode {oct(mode)}')
        return problems


# ------------------------------------------------------------ plan document

def _pins(config):
    return {
        'repo_commit': config.repo_commit,
        'fixture_repo_url': FIXTURE_REPO_URL,
        'image_digest': config.image_digest,
        'claude_version': config.claude_version,
        'node_version': config.node_version,
        'tmux_version': config.tmux_version,
        'runsc_version': config.runsc_version,
        'model_id': config.model_id,
    }


def _limits(config):
    return {
        'timeout_s': config.timeout_s,
        'max_turns': config.max_turns,
        'max_tokens': config.max_tokens,
        'budget_usd': config.budget_usd,
        'stop_method': config.stop_method,
        'rule': 'unknown cost / retries must never continue unbounded; the '
                'stop method fires at budget_usd regardless of whether the '
                'per-call cost is known',
    }


def _e05(config):
    digest = hashlib.sha256(MARKER_CONTENT.encode()).hexdigest()
    return {
        'status': NOT_RUN,
        'task': ('ONE small task only: Claude creates the marker file with '
                 'the fixed content inside the pinned public fixture '
                 'checkout; failure is diagnosed first, never auto-rerun as '
                 'a batch'),
        'marker': {
            'path': MARKER_PATH,
            'content': MARKER_CONTENT,
            'sha256': digest,
        },
        'verify': [
            'marker file content equals the fixed content byte-for-byte',
            f'sha256 of the marker equals the pinned digest {digest}',
            'Claude process exit code is recorded (0 expected; anything else '
            '= diagnose, no automatic retry)',
        ],
        'record': {
            'session_id': 'the ACTUAL Claude session ID of the run',
            'cwd': 'the cwd of the Claude process',
            'why': 'E10 resume needs both; without them resume is impossible',
        },
        'limits': _limits(config),
    }


def _e10(config):
    return {
        'status': NOT_RUN,
        'precondition': ('E05 completed with a recorded session ID + cwd and '
                         'the marker sha256'),
        'steps': [
            {'step': 'stop_and_remove',
             'action': ('docker stop + docker rm THIS run\'s container only '
                        '(label-scoped, own manifest entries)')},
            {'step': 'volumes_kept',
             'action': ('KEEP exactly the two approved volumes '
                        + ', '.join(f'{n} -> {d}' for n, d in APPROVED_VOLUMES)
                        + '; verify their labels before touching anything; '
                          'they are NEVER deleted during recreation')},
            {'step': 'recreate',
             'action': ('docker create a NEW instance from the SAME image '
                        f'digest {config.image_digest}, mounting the same two '
                        'volumes. ' + DOCKER_START_NOT_RECREATION)},
            {'step': 'identity_changed',
             'action': ('assert container identity/generation CHANGED: '
                        'container ID, State.StartedAt and cgroup path+inode '
                        'must all differ from the pre-stop instance; equal '
                        'values mean recreation did not happen')},
            {'step': 'marker_persisted',
             'action': 'the marker file is present in the new instance with the SAME sha256'},
            {'step': 'no_key_no_start',
             'action': ('new instance WITHOUT a key: Claude must NOT start '
                        '(ADR sandbox-lifecycle §6 缺 key 不啟動 Claude; '
                        'credentials_required semantics per '
                        'byok_policy.require_credential)')},
            {'step': 'reinject_and_resume',
             'action': ('safe ephemeral re-injection (tmpfs /run/credentials, '
                        'mode 0600, cleared on stop; value never in argv, '
                        'Docker persistent env, inspect output, logs, the '
                        'repo, the volumes or backups), then resume BY THE '
                        'EXPLICIT session ID recorded in E05, from the '
                        'recorded cwd')},
            {'step': 'conversation_verified',
             'action': ('assert the prior conversation CONTENT is actually '
                        'present in the resumed session; a fresh empty '
                        'session is resume FAILURE, not success')},
        ],
        'session_gate': {
            'no_key': 'credentials_required — Claude is not started',
            'key_and_session': 'resume by EXPLICIT session ID + recorded cwd',
            'key_session_missing': ('session_missing — explicit failure; a new '
                                    'session requires an explicit operator '
                                    'decision and is never reported as '
                                    'conversation continuation (#75: 不用「新程序'
                                    '成功」冒充對話接續)'),
        },
        'adr_quote': ADR_SESSION_QUOTE,
        'not_claimed': [
            'RAM / live process state survival',
            'tmux / socket / rootfs writable-layer survival',
            'docker start on the same container as recreation',
            'session-ID reconnection before the paid E05/E10 run proves it',
        ],
    }


def _launch_fixture_70(config):
    return {
        'status': NOT_RUN,
        'shared_base': ('same pinned image digest, fixture repo and cost '
                        'recording as #75; fault injection (paid pressure) '
                        'runs only under #70\'s own authorization, never by '
                        'default'),
        'launch_path': 'tmux (fixed session name) -> shell -> claude start',
        'record': [
            'process tree with parent-child PIDs at every stage',
            'exit codes of the tmux / shell / claude stages',
            'per-stage start/end timestamps',
        ],
        'outcome_classification': 'classify_pid_outcome(): the four-way '
                                  'taxonomy below, applied to the recorded observations',
        'four_way': [
            'new_command_eagain_rejection',
            'shell_exit',
            'agent_exit',
            'container_or_sentry_death',
        ],
        'invariant': ('tmux still alive is NEVER task success; success '
                      'requires the task\'s own completion evidence (exit '
                      'code + marker)'),
    }


def _evidence():
    return {
        'raw_location': ('a local private directory OUTSIDE the repository; '
                         'raw evidence is never committed'),
        'publish': ('scripts/gvisor-publish-evidence.py --raw <dir> --out '
                    '<fresh bundle> (redaction, per-file sha256 bundle, leak '
                    'scan)'),
        'checks': [
            'verify every published file hash against bundle-sha256.json',
            'secret scan must report leaks == [] before any commit',
            'a human reviews the bundle before it is committed',
        ],
    }


def _cleanup():
    return {
        'scope': (f'label {CLEANUP_LABEL_KEY}=<run_id> ONLY; resources this '
                  'run created (manifest written intent-before-mutation, '
                  'same idiom as gvisor-no-key-matrix.py)'),
        'steps': [
            'stop + rm own containers after verifying their label matches this run_id',
            'volumes: verified by label before rm, and removed only at FINAL '
            'cleanup after evidence sign-off (E10 recreation must keep them)',
            'refuse to touch any resource whose label does not match',
            'error if anything labeled with this run_id remains',
        ],
    }


def _cost_record():
    return {
        'fields': [
            'call_seq', 'phase', 'started_at', 'finished_at', 'model_id',
            'input_tokens', 'output_tokens',
            'cost_usd_known (bool: provider price known vs unknown)',
            'cost_usd (null when unknown — never silently estimated)',
            'provider_usage_raw_ref (pointer into raw evidence for cross-check)',
            'cumulative_spend_usd', 'budget_usd', 'stop_method_triggered',
        ],
        'cross_check': ('per-call usage is reconciled against the provider\'s '
                        'reported usage before the run is closed; totals that '
                        'disagree are recorded as a discrepancy, not rounded away'),
        'rule': ('起迄/退出 recorded per call; unknown cost still counts '
                 'against budget_usd; stop_method fires at the budget'),
    }


def build_plan(config):
    """The plan document for a VALIDATED config (no validation here)."""
    return {
        'schema': 'e5e10-plan/1',
        'status': NOT_RUN,
        'blocked': {
            'reason': BLOCKED_QUOTE,
            'missing': [
                'owner-provided LOCAL private key file location',
                'explicit model ID decision',
                'budget authorization (US$5 mention was a suggestion only)',
                'authorized execution host',
            ],
            'execution': ('intentionally absent: this script performs no '
                          'docker, network or model execution and mocks no '
                          'success path (repo rule: 不以 mock 假裝 Claude 成功)'),
        },
        'pins': _pins(config),
        'limits': _limits(config),
        'e05': _e05(config),
        'e10': _e10(config),
        'launch_fixture_70': _launch_fixture_70(config),
        'evidence': _evidence(),
        'cleanup': _cleanup(),
        'cost_record': _cost_record(),
    }


def plan(config):
    """Validated plan(): an unconfigured run cannot produce a plan."""
    problems = config.validate()
    if problems:
        raise ConfigError('refusing to plan an unconfigured run:\n  - '
                          + '\n  - '.join(problems))
    return build_plan(config)


def render_plan(document):
    """Text form. The NOT-RUN banner is impossible to mistake for a pass."""
    banner = '=' * 72
    return (f'{banner}\nE05/E10 EXPERIMENT PLAN — STATUS: NOT-RUN\n'
            f'NOTHING WAS EXECUTED: no docker, no network, no model calls.\n'
            f'{banner}\n'
            + json.dumps(document, indent=2, ensure_ascii=False)
            + f'\n{banner}\nEND OF PLAN — STILL NOT-RUN\n{banner}\n')


# ------------------------------------------------------ #70 PID taxonomy

OUTCOME_EAGAIN = 'new_command_eagain_rejection'
OUTCOME_SHELL_EXIT = 'shell_exit'
OUTCOME_AGENT_EXIT = 'agent_exit'
OUTCOME_CONTAINER_SENTRY_DEATH = 'container_or_sentry_death'
OUTCOME_NONE = 'no_failure_observed'
OUTCOMES = (OUTCOME_EAGAIN, OUTCOME_SHELL_EXIT, OUTCOME_AGENT_EXIT,
            OUTCOME_CONTAINER_SENTRY_DEATH)

_CLASSIFY_KEYS = ('eagain_seen', 'shell_pid_alive', 'agent_pid_alive',
                  'container_alive', 'sentry_events')
_NOT_SUCCESS_NOTE = ('tmux or container aliveness is never task success; '
                     'success requires the task\'s own completion evidence '
                     '(exit code + marker)')


def classify_pid_outcome(observations):
    """#70's four-way pressure taxonomy as a pure function.

    Precedence is most-severe first: a dead container explains the dead
    agent and shell under it, so container/Sentry death wins; then agent
    exit; then shell exit; the EAGAIN rejection is the benign case where a
    NEW command was refused but shell, agent and container survived.

    task_success is always False here BY DESIGN: process liveness (tmux
    included) is not task success (#70: 不能用tmux仍活代表Claude任務成功).
    Unknown extra keys (e.g. tmux_alive) are accepted and deliberately
    ignored.
    """
    missing = [key for key in _CLASSIFY_KEYS if key not in observations]
    if missing:
        raise ValueError('classify_pid_outcome: missing observation keys: '
                         + ', '.join(missing))
    if not observations['container_alive'] or observations['sentry_events']:
        outcome = OUTCOME_CONTAINER_SENTRY_DEATH
    elif not observations['agent_pid_alive']:
        outcome = OUTCOME_AGENT_EXIT
    elif not observations['shell_pid_alive']:
        outcome = OUTCOME_SHELL_EXIT
    elif observations['eagain_seen']:
        outcome = OUTCOME_EAGAIN
    else:
        outcome = OUTCOME_NONE
    return {'outcome': outcome, 'task_success': False, 'note': _NOT_SUCCESS_NOTE}


# ----------------------------------------------------------- session gate

def session_gate(has_key, session_record):
    """Gate the E10 resume decision. Never starts Claude without a key and
    never lets a missing session record become a silent new session.

    session_record is the E05 record: a dict with non-empty 'session_id'
    and 'cwd'. Anything else counts as session_missing.
    """
    if not has_key:
        return {
            'allowed': False,
            'code': 'credentials_required',
            'session_id': None,
            'note': ('Claude must not start without a key (ADR '
                     'sandbox-lifecycle §6 缺 key 不啟動 Claude; byok_policy '
                     'NO_KEY_REFUSE_START / CREDENTIALS_REQUIRED semantics)'),
        }
    record_ok = (isinstance(session_record, dict)
                 and isinstance(session_record.get('session_id'), str)
                 and session_record['session_id'].strip()
                 and isinstance(session_record.get('cwd'), str)
                 and session_record['cwd'].strip())
    if not record_ok:
        return {
            'allowed': False,
            'code': 'session_missing',
            'session_id': None,
            'note': ('explicit failure: no usable E05 session record, so '
                     'resume-by-session-ID is impossible; a NEW session '
                     'requires an explicit operator decision and must never '
                     'be reported as conversation continuation (#75: 不用「新程'
                     '序成功」冒充對話接續)'),
        }
    return {
        'allowed': True,
        'code': 'resume_by_explicit_session_id',
        'session_id': session_record['session_id'],
        'cwd': session_record['cwd'],
        'note': ('resume BY THE EXPLICIT session ID from the recorded cwd, '
                 'then assert the prior conversation content is present; '
                 'an empty/fresh session is resume failure'),
    }


# -------------------------------------------------------------------- CLI

def _build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--plan', action='store_true',
                        help='validate the config and emit the NOT-RUN experiment plan')
    parser.add_argument('--check-config', action='store_true',
                        help='validate the config only')
    parser.add_argument('--run', action='store_true',
                        help='intentionally rejected: execution is blocked pending '
                             'authorized host/key/budget (#75)')
    for name, help_text, arg_type in [
        ('--repo-commit', 'pinned full 40-hex commit of the fixture repo', None),
        ('--image-digest', 'immutable sha256:<64-hex> image digest', None),
        ('--claude-version', 'pinned claude CLI version', None),
        ('--node-version', 'pinned node version', None),
        ('--tmux-version', 'pinned tmux version', None),
        ('--runsc-version', 'pinned runsc version + platform', None),
        ('--model-id', 'explicit model ID (owner decision)', None),
        ('--timeout-s', 'per-run wall-clock timeout seconds', int),
        ('--max-turns', 'per-run turn limit', int),
        ('--max-tokens', 'per-run token limit', int),
        ('--budget-usd', 'per-run USD budget; unbounded runs are refused', float),
        ('--key-source', 'absolute path to the LOCAL private key file (0600, '
                         'outside the repo); the value never enters argv/env', None),
        ('--stop-method', 'how a run is stopped (整包費用停止方法)', None),
    ]:
        extra = {'type': arg_type} if arg_type else {}
        parser.add_argument(name, help=help_text, **extra)
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.run:
        print('REFUSED: --run does not exist in this slice. Execution is '
              'blocked pending an authorized host, an owner-provided local '
              'key location, an explicit model ID and budget authorization '
              f'({BLOCKED_QUOTE})', file=sys.stderr)
        return 2
    if not (args.plan or args.check_config):
        parser.error('choose --plan or --check-config (there is no --run: '
                     'execution is blocked per #75)')
    names = {f.name for f in fields(RunnerConfig)}
    config = RunnerConfig(**{name: getattr(args, name.replace('-', '_'))
                             for name in names})
    problems = config.validate()
    if problems:
        print('REFUSED — the run is not fully configured (#75 pins):',
              file=sys.stderr)
        for problem in problems:
            print(f'  - {problem}', file=sys.stderr)
        return 2
    if args.plan:
        print(render_plan(plan(config)), end='')
    else:
        print('config OK: fully pinned and limited; execution remains '
              'blocked per #75 (plan only via --plan)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
