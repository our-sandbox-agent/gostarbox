# Workspace persistence contract (preservation matrix)

Status: **Contract delivered; runtime implementation pending.** The #8 GO decision is not
lifted and the Go Runner (#11) is not started, so no volume, snapshot or sandbox runtime
exists to test against. This contract is a machine-checked preservation matrix distilled
from the [sandbox lifecycle ADR](../adr/sandbox-lifecycle.md) §4–§5 (Proposed), the
[memory/session recovery ADR](../adr/memory-session-recovery.md) (#67, Proposed) and the
[usage ledger ADR](../adr/usage-ledger.md) §2 (Proposed); the ADRs remain the source of
truth. **No runtime verification is claimed or performed.**

## Scope

[workspace-persistence.json](workspace-persistence.json) is the contract the Runner /
control plane must satisfy for workspace and home persistence;
`scripts/verify_persistence_contract.py` checks its internal consistency in the same
stdlib-only style as `scripts/verify_runner_contract.py`, and
`scripts/test_persistence_contract.py` guards every verifier rule with a
mutate-and-fail test.

It covers:

- Four resources with **independent resource IDs** (usage-ledger §2): `sandbox_runtime`,
  `workspace_volume`, `home_volume`, `snapshot` — each with its own ledger events;
  volume lifecycle is never derived from sandbox observed_state.
- Six lifecycle events — cold suspend, resume, destroy, memory-termination recreation
  (#67), volume resize, snapshot TTL expiry — each assigning every resource exactly one
  decision (preserve or drop): 6 × 4 = 24 decisions, each with a detail note and the
  acceptance check that guards it (issue #12 acceptance items, #77 metering check,
  work-image idempotent clone, ledger-examples snapshot case).
- What is always dropped with the runtime: RAM, processes, tmux/socket, rootfs writable
  layer, ephemeral credentials (API key in runtime tmpfs; CLI re-sends, else
  `credentials_required`). What survives suspend/recreation: approved workspace/home
  volumes, the recorded session ID and cwd, snapshots.
- Invariants: volume lifecycle independent from sandbox state (resize not derived from
  state); destroy does not imply snapshot deletion; retention deadline and actual
  release both recorded; suspend keeps storage meters running while compute stops only
  on the confirmed stop; credentials never persisted in volumes.
- Cold-restart expectations: no re-clone / no overwrite of an existing repo (work-image
  contract, [docs/contracts/work-image.md](work-image.md)), session ID stable across
  recreation (recreation evidence required, not a same-container `docker start`),
  cwd preserved, missing session reported as `session_missing`, first-boot init runs once.

## Non-goals

- No database, no real volumes, snapshots or runtime here — this repo delivers the
  contract only; Go implementation is pending the #8 GO and the #11 Runner.
- Warm suspend/checkpoint is out of scope (issue #12 本票不含; R-series research) and is
  refused by the lifecycle contract (422 `unsupported_suspend_mode`).
- No XFS reflink, loop volumes or Snapshot/Fork features (issue #12 本票不含).
- Backup/restore promises beyond the ADR's allowlist caveats; RPO is only committed
  after tested restore. Rootfs system packages are not preserved (writable layer dropped).

## Verify

```
python3 scripts/verify_persistence_contract.py
python3 -m unittest discover -s scripts -p 'test_*.py'
```

Both are pure-stdlib and offline.
