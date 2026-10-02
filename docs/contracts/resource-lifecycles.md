# Resource lifecycles: trash/purge, snapshots, sealed periods (#25)

Proposed, 2026-10-03. EXTENDS the #77 slice ([resource-events-slice.md](resource-events-slice.md),
`scripts/resource_events.py` — same envelope, same `EventLedger`, existing API
and tests unchanged) with the #25 lifecycle semantics and replayable fixtures
`docs/contracts/resource-lifecycle-fixtures.json`. Nothing here is deployed or
runtime-verified; this does not rewrite or re-count the #77 schema.

## What this adds over #77

| Concern | Semantics implemented |
|---|---|
| Volume trash/retention | `volume.trashed` marks the retention window START (payload may carry `retention_deadline_ms`); it is audit-only — the volume still exists and keeps accruing in trash. Only `volume.purged` ends accrual (a close event, like `resource.released`). Trash→purge is fully independent of sandbox resume/destroy: a destroyed or resumed sandbox never cuts, reopens or closes a trashed volume's interval. |
| Snapshot TTL | `snapshot.created/expired/deleted` already existed in the ADR §2 event set and #77 projection; confirmed here: TTL expiry (`snapshot.expired`) emits a delete REQUEST only — accrual ends solely at the confirmed `snapshot.deleted`. Both the retention deadline and the actual release time are recorded. |
| Snapshot placeholder | `snapshot_inventory()` returns an EMPTY inventory while no real snapshot operations are supported (current state) — usage is never fabricated. Real snapshot ops plug in there: derive the live set and confirmed sizes from `snapshot.created/deleted` events plus the storage service's own inventory (預留 contract，不虛構已用量). |
| Lost no-restatement | `apply_correction(events, sealed_periods)` applies recovery corrections with a billing-period watermark: a correction whose `effective_at_ms` falls inside a sealed (closed) period `[start_ms, end_ms)` is flagged for MANUAL handling and NOT auto-applied — sealed-period totals are never rewritten retroactively (恢復後不重疊、不回溯改已封帳數量). Corrections outside every sealed window append normally; envelope validation still runs before flagging. Flagged corrections never enter event history; the flag carries event id, period and effective time. |
| Period-scoped totals | `summarize(window=(start_ms, end_ms))` clips confirmed segments to a half-open window (uncertain ranges and open capacity stay unclipped) so sealed-period totals are checkable exactly. |
| Volume identity | An empty volume (quantity 0) accrues exactly zero usage but its totals row exists — the resource exists and is never omitted or fabricated. |

## Replayable fixtures

`docs/contracts/resource-lifecycle-fixtures.json` (ledger-examples idiom,
extended to full event cases); each carries events + hand-computed integer
expected totals + notes, replayed by
`scripts/test_resource_lifecycle_cases.py`:

| Fixture | Asserted semantics |
|---|---|
| `two-snapshots-delete-one` | 2 coexisting snapshots, different sizes/TTLs; deleting ONE (s1: 2048 B × 3000 ms = 6,144,000) leaves the other accruing to its own delete (s2: 4096 B × 7000 ms = 28,672,000). |
| `empty-volume-zero-usage` | 0-byte volume exists 5000 ms → row exists with exactly 0 usage. |
| `three-resume-cycles-no-double-count` | 3× suspend/resume: per-generation runtimes each 1,000,000 milliCPU·ms, volume 1024 B × 5000 ms straight through; full-batch redelivery duplicates. |
| `volume-trashed-while-sandbox-active` | Trash t3000 (audit-only) → purge t6000 ends volume at 6,144,000; sandbox compute continues to its own stop t8000 (8,000,000). |
| `snapshot-ttl-expired-delete-unconfirmed` | TTL expiry t2000 is a request; accrual to confirmed delete t6500 → 13,312,000 byte·ms. |
| `lost-correction-in-sealed-month` | Late trusted stop inside sealed 2026-09 → `flagged_manual`, not stored, sealed window stays 1,000,000 (auto-apply would restate 2,000,000). |

Targeted unit tests cover trash/purge independence (incl. purge after sandbox
destroy and across a resume), snapshot delete-confirmation, the sealed-period
boundaries (half-open) and multi-period watermarks, and the placeholder
inventory. Mutation guards prove each safeguard load-bearing: tampered fixture
totals fail; removing the sealed check restates the sealed window; treating
`volume.purged` as non-closing, `snapshot.expired` as a release, or
`volume.trashed` as an interval cut all break fixture totals.

## Runtime TODO (not built, not claimed)

- Real storage operations: no volume trash/purge backend exists; the events
  are the executable contract the future storage adapter (#11 runner,
  #76 control plane) will emit.
- Real snapshot system: no snapshot create/delete implementation exists;
  `snapshot_inventory()` stays empty until then — this ticket explicitly does
  NOT implement snapshot operations or multi-host storage (issue 本票不含).
- Real billing periods: sealing a period is a #25 billing concern
  (month snapshots with watermark/correction revision per usage-ledger §3);
  this slice only takes the watermark as a parameter and enforces
  no-restatement against it. Manual handling of flagged corrections is an ops
  runbook question that stays open here.

No Runner integration, no DB, no rates/currency, no end-to-end verification
has run; this is in-repo executable semantics plus fixtures only.

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/verify-ledger-examples.py
```
