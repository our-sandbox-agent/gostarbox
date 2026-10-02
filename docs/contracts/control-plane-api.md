# Control-plane API contract (M1 internal slice)

Status: **Contract + executable test double delivered; real backend not started.**
This is part 1 of issue #76's 分包順序: the machine-readable REST contract
[control-plane-api.json](control-plane-api.json) plus a stdlib-only executable
test double, in the repo's fixture+verifier idiom. Part 2 — wiring the real
TypeScript + Hono + Postgres service to the Go Runner — stays blocked on #11
(which still waits on the #8 GO). Nothing here is deployed, running, or
integrated with a Runner, CLI or Web frontend.

Sources of truth: [sandbox-lifecycle ADR](../adr/sandbox-lifecycle.md) §3 (the
unified `/v1` API — endpoint names and state/operation semantics are normative;
there is **no** second set of `/idle` `/suspend` `/resume` endpoints),
[usage-ledger ADR](../adr/usage-ledger.md) §1 (operation_id / generation /
ledger_seq envelope), [runner-lifecycle.json](runner-lifecycle.json) (state
machine + refusals this API fronts), [cli-surface.json](cli-surface.json)
(exit-code mapping targets) and [terminal-protocol.json](terminal-protocol.json)
(the `hello` frame the terminal ticket is presented at).

## What the double covers

`scripts/control_plane_double.py` is an in-memory `ControlPlaneDouble` with
`request(method, path, headers, body) -> (status, json)` — no HTTP server, no
external dependencies. `scripts/test_control_plane_contract.py` runs the issue's
required flow plus mutation-style guards (24 tests total):

- **Endpoints**: POST/GET `/v1/sandboxes`, GET `/v1/sandboxes/{id}`,
  POST `/v1/sandboxes/{id}/state`, DELETE `/v1/sandboxes/{id}`,
  GET `/v1/operations/{operation_id}`,
  POST `/v1/sandboxes/{id}/terminal-ticket`.
- **Operations**: 202 + operation resource (id, pending/running/succeeded/
  failed, expected_version, generation); poll to terminal; at most one change
  operation per sandbox; resume allocates a new generation; repeat destroy
  returns the existing operation.
- **Idempotency**: required on create/state/destroy; same key + same body →
  same operation (no double-create); same key + different body → 409
  `body_conflict`; the credential field never participates in body comparison;
  the map survives `snapshot()`/`restore()`.
- **Fencing**: expected_version mismatch → 409 `version_conflict`; the message
  tells the client to re-read.
- **State machine**: loaded from `runner-lifecycle.json`, never duplicated —
  refuse-on-unknown applies; acceptance moves observed only into the
  acceptance-driven intermediate states (Suspending/Resuming/Destroying);
  everything else waits for `confirm()` (the Runner-evidence stand-in).
- **Runtime honesty**: unconfirmed create stays observed `Creating` with a
  pending operation; lease expiry (`expire_lease()` hook) → `Lost`; never an
  optimistic Active.
- **Credentials**: resume without a key → 409 `credentials_required`, no
  operation, stays Suspend; secrets are never stored, echoed in operation
  payloads, or serialized into snapshots (only a `has_credential` boolean).
- **Capacity**: admission on create and resume → 429 `capacity_exceeded`,
  no operation created; compute returns only at the confirmed stop (Suspend),
  volumes only at confirmed Destroyed.
- **Auth/scope**: Bearer token on every endpoint, 401 otherwise; single tenant
  (one workspace), unknown/forged ids → 404 with no probe signal.
- **Restart**: `snapshot()`/`restore()` (plain JSON) keeps sandboxes,
  operations and idempotency queryable; terminal tickets are deliberately
  invalidated by a restart.
- **Guards**: 8 mutant subclasses (optimistic-Active, auth bypass, blind
  fingerprint, no dedup, no version fence, credential leak, Lost-without-fencing,
  no capacity accounting) each flip the exact invariant the flow tests rely on.

Known divergence, deliberate: the API edge maps admission rejection to **429**
`capacity_exceeded` (per this slice's instruction), while
`runner-lifecycle.json` records 409 for the runner-side refusal. The two layers
must be reconciled when the real backend lands (#11).

## What the real Hono/Postgres implementation must add

- **SQL schema preserving the identities** the ADRs fix (per
  `control-plane-api.json` + usage-ledger §1): `workspaces(id)`; `sandboxes(id,
  workspace_id, desired_state, observed_state, generation, fencing_token,
  version, runtime_deadline_at, last_confirmed_at, session_ref)`; `resources(id,
  kind, sandbox_id, quantity vector)` for workspace/home volumes and runtime;
  `operations(id, sandbox_id, type, state, expected_version, generation, target,
  result, error)`; `idempotency_keys(key, workspace_id, request fingerprint,
  operation_id, created_at)` with a uniqueness constraint; a ledger-event outbox
  (`ledger_seq` monotonic per resource, `operation_id`/`generation`/`source_seq`
  in the envelope) written in the same transaction as state.
- **Migrations re-runnable and non-destructive**: expand-contract steps, no
  data loss on re-run (issue acceptance), never rebuilding identities.
- **Binding**: listen on loopback or an SSH tunnel (or an otherwise verified
  trusted channel) only; the Runner management interface reachable only from
  the control plane; no auth request is served (401), tokens are stored hashed
  at rest and never enter logs; request logs redact credentials.
- **Operation progression**: a worker that moves operations through
  running→succeeded/failed on Runner evidence, reconciliation for timed-out
  requests (timeout ≠ not-executed), and Lost fencing per
  `runner-lifecycle.json`.
- **Real capacity admission** against host meters, emitting the
  reservation-vs-applied ledger events the double only models coarsely.

## Explicitly not done here

- Not deployed, not integrated: no Go Runner connection, no CLI/Web end-to-end
  (those are #14/#15 acceptance, which consume this contract; they are not
  prerequisites of this ticket).
- Workspace policy (`PUT /v1/workspaces/{id}/policy`) and deadline
  (`PATCH /v1/sandboxes/{id}/deadline`) are ADR §3 endpoints documented in the
  JSON but not exercised by the double.
- Multi-tenant authorization, invitations, OAuth: #16. Crash/failure
  reconciliation depth: #17. Public deployment: out of scope.

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 -m compileall -q scripts
```

Both are pure-stdlib and offline.
