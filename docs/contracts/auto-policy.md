# Auto idle/suspend policy machine (#19)

Proposed, 2026-10-03. In-repo, stdlib-only executable RULES of the #19
acceptance: `scripts/auto_policy.py` (`decide(state, signals, policy)`,
`countdown(policy, now, last_signal)`, `normalize_policy`), tested by the
61-row table in `scripts/test_auto_policy.py`. This is the SERVER-side
PRODUCT policy and is deliberately distinct from the browser DEMO clock
in `lifecycle.js` (that one is the GitHub Pages demo and excludes
closed-page time; this one is evaluated by the server from persisted
timestamps, so 瀏覽器關掉仍生效). Nothing here is wired to a runtime: no
WebSocket, no Runner, no CPU sampler, no hook transport is attached, and
no threshold constants are invented — an absent threshold disables its
transition entirely (ADR [sandbox-lifecycle.md](../adr/sandbox-lifecycle.md)
section 4: 自動門檻數值在 #19 以工作負載測試確定，未確定前不偷填常數).

## Signal taxonomy (terminal-protocol activity classes)

Signals are SEPARATE — one class per field, mapped to
[terminal-protocol.json](terminal-protocol.json) `activity_classification`:

| Signal | Source / activity class | Effect on the machine |
|---|---|---|
| `user_input` | terminal `input` frames; future IDE WebSocket client→server messages (#52) — class `user_input` (`counts_as_user_activity: true`) | PRIMARY idle signal: refreshes the countdown and wakes Idle→Active immediately |
| `ping` | `ping`/`pong` — class `liveness` | NEVER activity (liveness probe) |
| `resize` | `resize`/`resized` — class `layout` | NEVER activity (layout) |
| `output` | `output` frames — class `server_stream` | NEVER activity (server traffic; a streaming download does not keep the sandbox Active by itself) |
| `cpu_demand_milli` (+ `cpu_demand_process`) | Runner-side CPU sampling (ADR section 4 wakeup) | WAKEUP when numeric, positive and ≥ `cpu_demand_threshold_milli` — never ignored (2026-10-02 note), unless attributed to an excluded process |
| `new_task` | task-start hook | WAKEUP — never ignored |
| `busy` | busy hook report `{work_id, lease_expires_at, done, generation}` | busy protection (below) |
| `busy_hook_installed` | ops observation of the hook | `False` → hook 遺失 → `unknown_busy` + alert |
| `now` | injected clock | the only time source; `decide` reads no clock itself |

Session-control frames (`hello`, `hello_ack`, `hello_err`, `bye`) are
attachment lifecycle, not activity, and never reach this machine.
`hello`/`bye` churn from a flaky tab must not refresh the countdown.

## Busy-lease semantics (long task protection)

- A busy signal carries `work_id`, `lease_expires_at` and `done`.
  Renewal (續租) is a fresh report extending `lease_expires_at`; when
  reports stop arriving, the persisted claim's lease eventually expires.
- A VALID busy (work_id present, lease numeric and unexpired,
  `done=False`) FORBIDS both Active→Idle and Idle→Suspend. CPU need not
  be high: 2h+ 低 CPU 工作、下載與等待網路的受管工作 survive — the
  simulated 2h timeline is in the tests.
- Busy lease expired, hook missing (`busy_hook_installed: false`), or a
  malformed report (no work_id / non-numeric lease) → busy_status
  `unknown_busy`: KEEP the current state, emit the matching alert
  (`busy_lease_expired` / `busy_hook_missing` / `unknown_busy_report`)
  and `requires_human: true`. NEVER treated as done — expiry is checked
  BEFORE the done flag (缺漏、過期不視為任務完成).
- A completion report lifts protection ONLY when it matches the current
  generation and the claimed `work_id`; a late completion for another
  work id or from an old generation is ignored with
  `late_completion_ignored` / `stale_busy_report_ignored` (ADR section 4:
  晚到的舊完成訊號不能蓋掉新 busy; the #17 generation-fencing convention).
- Full session/task/序號 verification of hook reports is runtime wiring
  (below); the machine provides the generation + work_id admission
  surface for it.

## Conservative defaults

- Unknown observed state or `now`, a missing activity anchor (no
  `last_user_input_at` / `last_wakeup_at` / `state_since`), a present-
  but-unrecognized signal value, or any `unknown_busy` → keep the
  current state over demotion. 不確定時保守保留.
- `idle_after_seconds` / `suspend_after_seconds` `None` (or the matching
  `auto_*_enabled: false`) disables that transition — no constants are
  filled in by default; invalid policy values raise `ValueError`.
- Quota-reached shutdown is NOT this machine: 配額達限另走明確停機政策
  (#18 quota/kill, `runtime_deadline_at` with reason `runtime_limit`).

## State immunity

- `Error`, `Lost` — and every observed_state outside `{Active, Idle}` —
  get NO auto transition at all: timers and wakeups never override them,
  and no dead task is auto-replayed (2026-10-02 note).
- A pending operation (suspend/resume/destroy in flight) blocks every
  new auto transition (`operation_in_flight`): one change operation per
  sandbox (#17); the reconciler converges desired vs observed there.
- Cooldown is BETWEEN demotions (`last_demotion_at` +
  `demotion_cooldown_seconds`): it delays the next demotion but NEVER
  blocks a manual input wake — wake checks run before the cooldown gate.
- IDE exclusion: `policy.excluded_processes` (e.g. `code-server`) — CPU
  demand attributed to an excluded resident process is not a wakeup, so
  an IDE heartbeat cannot keep the sandbox from ever demoting
  (2026-09-22 founder note). This ticket is terminal-first; the list is
  the reserved interface for #52.

## Server authority & countdown

`decide` and `countdown` are pure functions evaluated on the server from
persisted state (`signals.now` is the only clock). The browser renders
but never computes policy — a closed browser changes nothing.
`countdown(policy, now, last_signal)` returns
`{idle_in, suspend_in}` seconds-to-next-demotion anchored at the same
last-signal timestamp; the caller picks the field matching
`observed_state`. Unknown inputs or disabled transitions return `null`
counts (ADR section 3: `next_transition_at` 無倒數為 null). Decisions are
PROPOSALS (`request_idle` / `request_suspend` / `wake_active`); the
caller drives them through the existing operation path of
[runner-lifecycle.json](runner-lifecycle.json) — this module performs no
transition itself.

## Cold-resume copy rule

Cold suspend stops processes; resume starts NEW ones (no RAM, PID,
socket or original tmux screen is preserved — ADR section 1 and the
[workspace-persistence](workspace-persistence.md) contract). Product copy
around auto-suspend must therefore 不宣稱同程序續跑: say "files kept,
processes restarted", never "your task keeps running". The replay after
resume shows the new session screen (terminal-protocol `cold_resume`).

## Boundaries

- **#17**: recovery workflow (recreate after Error/Lost, reconciliation
  verdicts, generation bumping, operation serialization) lives in
  [operation-reconcile.md](operation-reconcile.md) and is NOT duplicated
  here; this machine only refuses to act while an operation is pending
  or the state is Error/Lost.
- **#18**: quota enforcement, watchdog kill and quota restore live in
  [watchdog-lease.md](watchdog-lease.md); the explicit stop path for
  limits is `runtime_deadline_at` (`runtime_limit`), never idle timers.
- **#52 (IDE)**: IDE-mode user input joins `user_input` as a WebSocket
  client→server message; `excluded_processes` is the reserved interface.
- **Demo**: `lifecycle.js` / `lifecycle.test.js` remain the browser demo
  clock only — no shared code, no shared defaults.

## Runtime TODO (blocked; NOT claimed)

- Real signal wiring: terminal WebSocket `input`/`ping`/`resize`/`output`
  classification (#13), IDE WebSocket messages (#52), Runner CPU demand
  sampling including throttling-aware metrics (ADR section 4 — CPU 判定
  包含限速造成的 throttling 指標), and the busy/done hook transport with
  full generation/session/task/序號 verification.
- Threshold values from workload testing (ADR section 4); UI countdown
  display consuming `countdown`; ops routing for `requires_human`
  unknown-busy alerts.
- Cross-ticket integration with #17 operations and #18 quota paths.
- Parallel/subtask and permission-wait blocking of Suspend beyond the
  busy hook (ADR section 4) once those signals exist at runtime.

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
```
