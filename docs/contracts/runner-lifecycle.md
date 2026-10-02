# Runner lifecycle transition contract

Status: **Contract delivered; runtime implementation pending.** The Go Runner (#11) is not
started and the #8 GO decision is not lifted. This contract is a machine-checked table
distilled from the [sandbox lifecycle ADR](../adr/sandbox-lifecycle.md) (Proposed) and the
[memory/session recovery ADR](../adr/memory-session-recovery.md) (#67, Proposed); the ADRs
remain the source of truth. **No runtime verification is claimed or performed.**

[runner-lifecycle.json](runner-lifecycle.json) is the shared contract the Go runner must
satisfy; `scripts/verify_runner_contract.py` checks its internal consistency, in the same
stdlib-only style as `scripts/verify-ledger-examples.py`.

## What it covers

- The 10 `observed_state` states (Creating, Active, Idle, Suspending, Suspend, Resuming,
  Destroying, Destroyed, Lost, Error) and 31 allowed transitions, each with trigger
  (operation type), `requires` guards (confirmed observation, credentials present,
  expected_version, single change operation) and `side_effects` (compute interval closes
  only at confirmed stop; volumes retained in Suspend/Error; capacity released only after
  confirmed removal).
- Error discipline: `memory_limit_terminated` only on confirmed memory termination;
  `recovery_retry_exhausted` only on the Resuming→Error restart path; Error never
  silently becomes Idle/Active — recovery goes through reconcile → state endpoint →
  Resuming → new generation.
- Lost rules: entered by lease expiry from any unfinished state; keeps its resource
  reservation (no auto-release); never goes directly to Destroyed or recreates the same
  sandbox; every exit requires fencing/reconciliation.
- Fencing/generation rules reference the [usage ledger ADR](../adr/usage-ledger.md):
  new generation + monotonically increasing fencing token per execution-instance
  replacement; stale generations cannot submit state or events.
- Capacity admission guard inputs on create/resume: milliCPU, memory bytes, volume
  bytes; exceeded → 409 `capacity_exceeded`, no operation, no transition.
- Explicit refusals (refuse-on-unknown): warm suspend (422 `unsupported_suspend_mode`),
  missing credentials (409 `credentials_required`), version/operation conflicts,
  same-key-different-body, changes during Suspending/Resuming, actions on Error before
  reconcile, actions on Lost before fencing, stale generation, create validation, and
  unknown operations — none of which create an operation or transition.

## What it does NOT cover

- Real gVisor/Docker execution, adapters, PID-limit enforcement (`--ulimit nproc`,
  cap-drop, host pids backstop) — #11 runtime acceptance, blocked on the #8 GO.
- Watchdog timing, lease lengths, reconcile scheduling — #17 operation/fencing/reconcile
  and #21/#22 watchdog hardening.
- Persistence of operations/events, usage metering storage — #12/#18 and the
  usage-ledger implementation tickets.
- Automatic recovery strategy (#19 product decision; trial default remains explicit
  user restart).

## Verify

```
python3 scripts/verify_runner_contract.py
python3 -m unittest discover -s scripts -p 'test_*.py'
```

Both are pure-stdlib and offline. The unittest file also contains guard tests that
mutate the contract in-memory and assert the verifier rejects each broken rule
(required by CONTRIBUTING).
