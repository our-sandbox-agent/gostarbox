# Watchdog lease and quota restore slice (#18)

Proposed, 2026-10-03. In-repo, stdlib-only executable RULES of the #18
acceptance: `scripts/watchdog_lease.py` (`Lease`, `WatchdogPolicy`,
`quota_restore` / `memory_max` / `pids_max`, `network_failure_policy`),
tested by `scripts/test_watchdog_lease.py`. Real host watchdog integration
is blocked on #11/#12; nothing here is deployed, this module writes no
sysfs file, kills no process and makes no runtime claims. It REUSES the
existing conventions — fencing generations per
[operation-reconcile.md](operation-reconcile.md) (#17) and the uncertain
usage intervals of [resource-events-slice.md](resource-events-slice.md) —
it does not define a second schema or a parallel state machine.

## Delivered rules

| Concern | Semantics implemented |
|---|---|
| Lease / fencing epochs | `Lease(sandbox_id, clock, ttl_ms)`: the epoch counter is the per-sandbox fencing token, monotonically increasing across runner (re)creations (the [#17](operation-reconcile.md) generation convention). `issue()` grants a new epoch and fences every older one; `renew(epoch)` accepts only the exact current epoch — a stale runner after recreation cannot keep the old lease alive (the old runner's lease cannot extend past the new instance's). `expired_at(now)` is half-open: expired once `now >= deadline_at`. Renew of an already-expired lease is fenced (`LeaseExpired`): recovery is a new epoch, never zombie resurrection. |
| Detect → confirm stop | `WatchdogPolicy.evaluate(now, lease_state, runner_reachable)` is a pure decision (the Reconciler.diff convention: deciding is separate from doing). Lease expiry records T_detect (`detected_at`); a confirmed stop requires EVIDENCE — watchdog kill confirmation or host-level stopped-process observation. A dead runner's own report is NEVER proof (不能用已停止的 runner 證明必定會 suspend): `runner_confirmed_stop` counts only from a reachable runner, and runner-unreachable alone is not confirmation. Evidence from a stale epoch is not proof (a recreated instance is not evidence, #17 convention). |
| Three-timestamp billing discipline | `detected_at <= confirmed_stopped_at = billing_cutoff_at` (equality allowed). The billing cutoff is the confirmed stop ONLY: between detect and confirm the decision is `lost_unconfirmed` — display Lost/Unknown (never Active), `release_capacity: false` (never free), `billing_cutoff_at: null`. This matches the Lost uncertain intervals (H→R, never billed as confirmed, never zero) of [resource-events-slice.md](resource-events-slice.md); closing the billing interval at confirmation emits `runtime.stopped` into that shared ledger in the real backend. |
| Persistent loss escalation | Unconfirmed past `confirm_timeout_ms` (default 30s): `requires_human: true` + `stop_unconfirmed_escalation` alert — runner 持續失聯有明確告警與人工處置. |
| Watchdog itself failed | `watchdog_unreachable_alert(now)` is a DISTINCT path: flag `watchdog_failed`, display Unknown, `requires_human: true`. It never derives a stop confirmation, billing cutoff or capacity release from watchdog failure (Lost 計 0 不代表主機已停 — issue #18 本票不含). |
| Quota restore (never `max`) | `quota_restore(cpu_milli, period_us=100000)` builds the cgroup v2 `cpu.max` VALUE `"<MAX> <PERIOD>"` restoring the PURCHASED quota. Per the [kernel cgroup-v2 doc](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html), `cpu.max` is "$MAX $PERIOD" where MAX is µs of CPU time per period and the literal `max` means UNLIMITED — the original plan-detail 3.4 step of restoring `max` would break the purchased CPU bound (issue #18 note). Restore NEVER emits `max`: MAX = `cpu_milli * period_us // 1000` (µs; floor division never grants more than purchased), e.g. 2000 milliCPU → `"200000 100000"`, 500 milliCPU → `"50000 100000"`. `memory_max(bytes)` and `pids_max(n)` are plain value builders. Pure string builders: no sysfs writes. |
| Network-failure isolation | `network_failure_policy(stage, ...)`: network cleanup/flush failure returns the explicit action `block_and_isolate` — block and isolate for human decision, volumes KEPT, `destroy: false`. A data-deletion destroy is NEVER triggered from a cleanup failure (no destroy-with-delete semantics). No billing cutoff is derived from a network failure. |

## Decision shape

`evaluate` / `watchdog_unreachable_alert` return a decision dict the
caller persists: `phase` (`healthy` | `lost_unconfirmed` |
`confirmed_stopped` | `watchdog_unreachable`), `display_state`,
`detected_at`, `confirmed_stopped_at`, `billing_cutoff_at`,
`release_capacity`, `alerts`, `actions`, `requires_human`,
`evidence_rejected` (reason when evidence was presented but inadmissible:
`dead_runner_not_proof` | `unknown_proof_source` | `stale_epoch_not_proof`).

## Runtime TODO (blocked; NOT claimed)

- Real `kill -9` runner test and control-plane link-cut test (issue
  acceptance) — needs the #11 host runtime; the acceptance criterion is
  NOT covered by this library.
- Multi-sandbox pressure tests per the 2026-10-02 issue note: at least
  two sandboxes created simultaneously, CPU/memory/PID/disk pressure and
  runner loss; verify one termination does not spread and capacity is
  returned only after confirmed stop. The single-sandbox ordering of #71
  does not replace this admission test.
- Host-only management UID checks with #11/#17: the host-only management
  UID must not be obtainable from any guest entry point.
- Real cgroup v2 writes (`cpu.max`, `memory.max`, `pids.max` values from
  the builders above), watchdog process supervision and its alerts.

## Boundaries

- **#17**: recreation after Lost/Error, reconciliation verdicts and
  generation bumping at acceptance live in
  [operation-reconcile.md](operation-reconcile.md); this slice supplies
  the host-side lease/expiry/fencing rules that feed it.
- **#19**: automatic recovery/restart strategy is a product decision;
  `requires_human` escalations here only open the human path.
- **#25/#77**: rates, currency and unresolved-interval billing gates stay
  there; this slice only decides when the cutoff timestamp may exist.
- No high-availability cluster (issue 本票不含).

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
```
