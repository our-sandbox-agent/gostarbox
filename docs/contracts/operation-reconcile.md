# Operation reconcile slice (#17)

Proposed, 2026-10-03. In-repo, stdlib-only executable semantics of the
control-plane half of the #72 recovery contract: `scripts/operation_reconcile.py`
(`OperationLog`, `Reconciler`, `RuntimeDouble`, `simulate_crash`), tested by
`scripts/test_operation_reconcile.py`. Real Runner integration is blocked on
#11; nothing here is deployed or runtime-verified. This reuses the EXISTING
modules and schemas — operation/state semantics per
[control-plane-api.json](control-plane-api.json) and
[runner-lifecycle.json](runner-lifecycle.json), the durable event trail and
state+outbox commit per [resource-events-slice.md](resource-events-slice.md)
(`EventLedger`, same envelope, `operation.*` / `lease.expired` types) — it
does not define a second schema or a parallel state machine.

## Delivered rules

| Concern | Semantics implemented |
|---|---|
| Persisted-first | The intention row is persisted BEFORE runtime execution: `requested → dispatched → confirmed/failed/timeout`. Acceptance alone moves observed state only into the intermediate state (Suspending/Resuming/Destroying); the confirmed target comes only from the runtime verdict. |
| Serialization | One change operation per sandbox at a time (per-sandbox single sequence). An opposite operation while one is pending answers 409 `opposite_operation` and never interleaves; a same-type retry replays the SAME operation. `expected_version` mismatch answers 409 `version_conflict` with no operation created. |
| Idempotency | One `Idempotency-Key` maps to at most one intention row (the dedupe key); same key + same body replays the original operation, same key + different body raises 409 `body_conflict`. Repeat destroy after Destroyed returns the existing operation, never a second one. |
| Timeout ≠ failed | `mark_timeout` records outcome-UNKNOWN: the operation stays pollable and re-dispatchable (`retryable: true`, `timeout_is_not_failed: true`); the sandbox is not failed. Failure needs a confirmed verdict (runtime outcome, or the reconciliation verdict `lost_reconciled`). |
| Runtime-ok / DB-fail | If the runtime effect happened but the verdict write was lost, replay re-executes through the idempotent runtime (intention id = dedupe key): same single outcome, `effects == 1`, no double-build. |
| Fencing | Every resume/recreation bumps the sandbox generation (monotonic fencing token) at acceptance. Writes (`dispatch`, `runtime_outcome`, `mark_timeout`) and events carrying an old generation are REJECTED for current state (`stale_generation` / `history_only`) but PRESERVED as history — late old-generation events never mutate current state (runner-lifecycle `generation_fencing`). |
| Lost | `mark_lost` keeps the reservation (`lost_keeps_reservation`), marks usage uncertain (never zero, never confirmed) and emits `lease.expired` into the shared ledger. State changes from Error/Lost require the reconciliation verdict first (`mark_reconciled`), which also resolves any outstanding operation. |
| Reconciler | `diff(desired, observed)` → auditable actions, `apply()` drives them through the log: desired Suspend + observed Active → `request_suspend`; unknown instance (observed, no desired record) → `isolate_unknown_instance` + human flag, NEVER auto-adopted; Lost without stop evidence → `keep_reservation_mark_uncertain`; recreation → `recreate_new_generation` (Error→Resuming→Active, generation bump fences the old instance). |

## Crash matrix (executable spec)

`simulate_crash(op_type, point)` runs the persisted-first pipeline, dies at
`point`, restores the last snapshot into a fresh `OperationLog` (`from_dict`),
replays, and compares against a no-crash run — sandbox count, per-sandbox
observed/desired/generation/reserved, every operation state, ledger event
count and runtime effect count must be EQUAL (`python3 scripts/operation_reconcile.py`):

| point \ op | suspend | resume | destroy |
|---|---|---|---|
| after_intention | converged | converged | converged |
| after_dispatch | converged | converged | converged |
| after_runtime_before_confirm | converged | converged | converged |
| after_confirm_before_event | converged | converged | converged |

12/12 cells verified in `test_full_crash_matrix_converges`
(`scripts/test_operation_reconcile.py`), including `runtime_executions == 1`
in every cell (no double-build across the crash). Deterministic event ids
(`{operation_id}:{event_type}`) make re-emission after `after_confirm_before_event`
a ledger dedupe, never a second event.

## Real control-plane additions (blocked on #11)

This library is the executable semantics, not a runtime. The real backend
(TypeScript + Hono + Postgres per the language decision) additionally needs,
when #11 lands:

- intention/verdict/outbox rows written in ONE Postgres transaction (the
  in-memory `to_dict` snapshot stands in for that commit boundary);
- an outbox worker delivering `operation.succeeded/failed` exactly once
  (delivery state is already markable on the shared `EventLedger` outbox);
- real Runner calls with per-runner classification of unknown deaths (#11);
  HTTP 202 is acceptance, never success — the double in
  `scripts/control_plane_double.py` already enforces the same refusal table.

No runtime claims are made here: no Runner exists, no end-to-end
split/restart fault injection has run, and acceptance criterion "split/restart
故障測試" is only covered by the in-replay crash matrix above until #11.

## Boundaries

- **#18 (watchdog/lease)**: host-side lease expiry detection and fencing of
  old generations on the host belong to the watchdog ticket; this slice only
  records the control-plane consequence (Lost + reservation kept + uncertain).
- **#19 (auto policy)**: any automatic recovery/restart strategy, idle and
  runtime-limit countdowns are product decisions owned by #19; this slice
  recreates only on an explicit desired-state diff.
- **#25/#77 (billing/ledger)**: rate cards, pricing and unresolved-interval
  billing gates stay there; this slice only appends to the shared ledger.
- Not in #17 (per the issue): multi-runner schedulers, Kafka, distributed
  lock services.

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/operation_reconcile.py
```
