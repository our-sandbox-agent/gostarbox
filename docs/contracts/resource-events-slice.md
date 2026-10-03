# Resource-events minimal slice (#77)

Proposed, 2026-10-03. In-repo, stdlib-only executable semantics of
[docs/adr/usage-ledger.md](../adr/usage-ledger.md) §1–§3: `scripts/resource_events.py`
(`EventLedger`), tested by `scripts/test_resource_events.py`. Real Runner
integration is blocked on #11; nothing here is deployed or runtime-verified.
This extends the EXISTING ledger contract — same envelope, same fixtures
(`docs/contracts/ledger-examples.json`, reused by tests, not forked) — it does
not define a second schema.

## What this slice fixes

| Concern | Semantics implemented |
|---|---|
| Envelope | Immutable events per usage-ledger §1 (`schema_version … certainty`). Validation on append: required fields, integer ms times and integer quantity vectors (no floats/JS Number), `certainty ∈ {confirmed, uncertain}`, known event types. `effective_at_ms` is required — never fabricated from `recorded_at_ms`/arrival. |
| Ordering | `ledger_seq` is a per-(resource_type, resource_id) monotonic counter assigned by the ledger; a caller-supplied value must equal the next one. Projection folds by (effective time, append order); same-instant cuts close the old vector before opening the new one. |
| Dedupe | Exact redelivery (same `event_id`, or same `(source_id, generation, source_seq)`, identical content ignoring control-plane-assigned fields) returns the original receipt — no second event, no double count. Same key with different content is quarantined and flagged; stored history is never overwritten. |
| Projection | Half-open `[start, end)` confirmed intervals per resource — runtime, workspace_volume, home_volume, snapshot each keep their own `resource_id` (volume lifecycle never derived from sandbox state). State change / same-state resize / release cut segments; zero-length segments are zero. Cold suspend closes compute only at the confirmed `runtime.stopped`; volumes keep accruing; `snapshot.expired` is a delete request (audit), only `snapshot.deleted`/`resource.released` end storage; sandbox destroy never assumes snapshot deletion. |
| Uncertainty | `lease.expired` closes the confirmed interval at the last trusted heartbeat H and marks `[H, R)` uncertain — counted as uncertain ms + retained capacity, never as Active, never as zero, never in confirmed totals. A trusted late stop/resize inside the gap is a correction: the projection recomputes from effective time and BOTH versions are kept (`superseded()` archive). |
| Totals | `summarize()` reports per-resource integer `quantity × ms` per meter (milliCPU·ms, byte·ms) plus uncertain ranges and open capacity ONLY. No rates, no currency, no dollar figures: there are no approved rate cards, so none are displayed or stored. |
| Outbox | `commit_with_outbox(event, payload)` appends the state confirmation and its outbox entry under one atomic sequence id (in-memory stand-in for the #76 same-DB-transaction write). Redelivery returns the original receipt and never creates a second entry; delivery state is markable. |
| Persistence | `to_json()` / `from_json()` round-trip the append-only event list (plus quarantine, flags, outbox); restore replays the events, so replay and restart yield identical totals and continued ledger_seq. |

## What #25 keeps

Rate cards and `rate_id` versions, billing lines and month-boundary
aggregation, round-half-up minor-unit pricing, month snapshots with
watermark/correction revision, formal Lost reconciliation runbooks and
manual-resolution ops, Stripe/prices. The two rate-bearing fixture cases
(`expected_minor`) are intentionally NOT priced by this slice. Late
generation fencing writes and unresolved-interval billing gates also stay
with #25/#17.

## Integration point

The Runner (#11) emits these events; the control plane (#76) validates and
persists them in the same DB transaction as the state confirmation and the
outbox row. Runtime execution and DB commits have NO cross-system atomicity:
unconfirmed outcomes stay pending/uncertain — the ledger never converts an
accepted request into applied resource quantity. External API idempotency
keys and ledger event dedupe remain two separate layers (usage-ledger §1).

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/verify-ledger-examples.py
```
