# E05/E10 gated experiment runner (#75, #70)

Status: **Preparation delivered; execution BLOCKED.** Per #75 line 4:
「Blocked：尚缺擁有者提供專用 API key 的本機配置位置、明確模型 ID、費用預算與停止門檻。聊天提過 US$5 只是建議，未視為付款或模型呼叫授權。準備無 key 的腳本/fixture 可先做，付費執行待配置及授權。」
This slice is the sanctioned 無 key part only: the gated planner, fixtures
and classification logic. No docker, network or model execution exists in
it, and no success path is mocked (repo rule: 不以 mock 假裝 Claude 成功).

Implementation: `scripts/e5e10_runner.py` (stdlib only); tests:
`scripts/test_e5e10_runner.py` (41 tests incl. 6 mutate-and-fail guards,
CONTRIBUTING idiom).

Sources of truth: issues #75/#70 (both under #8 / M0),
[ADR memory-session-recovery](../adr/memory-session-recovery.md) (Proposed;
E05/E10 recreation + session semantics), [ADR sandbox-lifecycle](../adr/sandbox-lifecycle.md)
§6 (缺 key 不啟動 Claude), [byok-policy.md](byok-policy.md)
(`credentials_required` / `NO_KEY_REFUSE_START` / ephemeral tmpfs
injection), [work-image.md](work-image.md) (pinned digest + fixture repo),
`scripts/gvisor-no-key-matrix.py` + `scripts/gvisor-m0-memory.py`
(label-scoped manifest/cleanup idiom), `scripts/gvisor-publish-evidence.py`
(publishing boundary).

## What the runner prepares

`RunnerConfig` + `validate()` — an unconfigured run is impossible. Every
pin and limit below must be set before a plan can even be emitted;
`plan(config)` raises `ConfigError` and the CLI exits 2 naming each
problem precisely. There is no dry-run-success output: every plan is
marked `not_run` top to bottom, and text plans carry a
`STATUS: NOT-RUN / NOTHING WAS EXECUTED` banner.

| gate | rule |
|---|---|
| repo_commit | full 40-hex commit of the fixture repo (`our-sandbox-agent/sandbox-console`, the #70/#75 shared fixture) |
| image_digest | immutable `sha256:<64-hex>`; `docker start` of an old container never substitutes |
| claude/node/tmux/runsc versions | all pinned non-empty |
| model_id | explicit non-empty (owner decision; a US$5 chat mention is not authorization) |
| timeout_s / max_turns / max_tokens | positive integers, per run |
| budget_usd | positive number, per run — 未設預算即拒絕 |
| stop_method | non-empty description of the 整包費用停止方法 — 未設停止方法即拒絕 |
| key_source | absolute path to an existing LOCAL private file, mode 0600, OUTSIDE this repo. The key VALUE never enters argv, env, Docker persistent env/inspect, logs, the repo, the workspace/home volumes or backups; this slice never reads the file content, only checks the path/mode |

`plan(config)` — the experiment plan document (dict + text):

- **E05** (one small task): Claude creates `/workspace/e05-marker.txt`
  with fixed content `gostarbox-e5-marker-v1\n` inside the pinned public
  fixture checkout; verification = byte-for-byte content + pinned sha256 +
  recorded exit code; the run records the ACTUAL session ID and cwd
  (both required by E10); failure is diagnosed first, never auto-rerun.
- **E10** (cold recreation + resume): stop+rm this run's container ONLY;
  KEEP exactly the two approved volumes (`workspace → /workspace`,
  `home → /home/node`, verified by label, never deleted during
  recreation); create a NEW instance from the same digest; ASSERT
  identity/generation changed (container ID, `State.StartedAt`, cgroup
  path+inode must all differ — `docker start` of the same container is
  NOT recreation, it retains identity + writable layer per the ADR);
  marker present with the same sha256; WITHOUT key Claude must NOT start
  (`credentials_required`); after safe ephemeral re-injection, resume BY
  THE EXPLICIT session ID from the recorded cwd and assert the prior
  conversation CONTENT is present.
- **#70 launch fixture**: the planned tmux → shell → Claude start path,
  recording the process tree with parent-child PIDs, per-stage exit codes
  and timestamps, on the same pinned image/fixture/cost base; fault
  injection (paid pressure) runs only under #70's own authorization.
- **evidence / cleanup / cost** discipline (below).

`classify_pid_outcome(observations)` — #70's four-way taxonomy as a pure
function over `{eagain_seen, shell_pid_alive, agent_pid_alive,
container_alive, sentry_events}`, precedence most-severe first:

1. `container_or_sentry_death` (container dead or sentry events)
2. `agent_exit` (Claude process dead)
3. `shell_exit`
4. `new_command_eagain_rejection` (new command refused; shell/agent/container alive)
5. `no_failure_observed` (nothing failed — still not success)

`task_success` is **always False** here by design: tmux (or container)
aliveness is never task success (#70: 不能用tmux仍活代表Claude任務成功);
success requires the task's own completion evidence (exit code + marker).

`session_gate(has_key, session_record)`:

| situation | verdict |
|---|---|
| no key (any record) | `credentials_required` — Claude is not started (ADR §6; byok `NO_KEY_REFUSE_START`/`CREDENTIALS_REQUIRED`) |
| key + record with `session_id` + `cwd` | `resume_by_explicit_session_id` — echo both |
| key + record missing/unusable | `session_missing` — explicit failure; a new session requires an explicit operator decision and is never reported as conversation continuation (#75: 不用「新程序成功」冒充對話接續) |

## Hard gates (execution blocked)

CLI modes are `--check-config` and `--plan` only. **There is no `--run`**:
it exists only to be refused with exit 2 and the #75 block quote, pending
all of:

1. an authorized execution host,
2. the owner-provided LOCAL private key file location,
3. an explicit model ID decision,
4. budget + stop authorization (US$5 was a suggestion, not authorization).

Per #75,先用假秘密驗憑證注入/清理與發布器 is the flow to rehearse before
any real key exists; the real key only ever travels local-private-file →
ephemeral tmpfs injection and never lands in argv, Docker persistent
env/inspect, logs, the repo, the workspace/home volumes or general
backups, and is never pasted into issues/PRs.

## Shared fixture base with #70

#75 and #70 share the pinned image digest, the public fixture repo +
commit, the tmux→shell→Claude launch fixture, and the cost record. #75
does not depend on #70; #70's fault injection executes only under its own
paid-stop authorization and never by default (per #70: 成本/timeout/次數限
制與清理/發布走#75機制).

## Evidence / cleanup / cost discipline

- Raw evidence stays in a local private directory OUTSIDE the repo;
  publish via `scripts/gvisor-publish-evidence.py` (redaction +
  `bundle-sha256.json` + leak scan must report `leaks == []`); human
  review before commit; RAM/tmux/socket/rootfs-layer persistence is never
  claimed.
- Cleanup is label-scoped (`sandbox.spike=<run_id>`) to resources this run
  created (manifest written intent-before-mutation, matrix idiom); labels
  verified before any removal; volumes removed only at FINAL cleanup
  after evidence sign-off; leftover labeled resources are an error.
- Cost record per call: `call_seq, phase, started_at/finished_at,
  model_id, input/output_tokens, cost_usd_known, cost_usd (null when
  unknown — never silently estimated), provider_usage_raw_ref,
  cumulative_spend_usd, budget_usd, stop_method_triggered`; per-call usage
  cross-checked against provider-reported usage; unknown cost still counts
  against the budget; the stop method fires at `budget_usd` so retries
  never continue unbounded.

## No-runtime-claims

This slice claims NOTHING runtime: no docker execution, no model call, no
credential injection, no session resume. Claude session-ID reconnection
stays unverified until the paid E05/E10 run proves it — ADR
memory-session-recovery: 「Keeping files does not prove a Claude process
resumes.」 Completing #75 only adds execution evidence; #8's human GO,
#10's official image and external trial do not auto-complete.
