# Minimal ops alerting and backup/retention rules slice (#23)

Proposed, 2026-10-03. In-repo, stdlib-only executable RULES of the #23
acceptance (deploy/backup-restore/minimal ops alerting):
`scripts/ops_alerting.py` (`AlertRules`, `RunbookRegistry`,
`BackupPolicy`, `RetentionPolicy`, `DiskAdmission`, `DataDirInit`),
tested by `scripts/test_ops_alerting.py` (mutate-and-fail guards
included). Nothing here runs a probe, sends an alert, performs a backup,
writes a disk or deletes a volume — no runtime claims. Per the
2026-10-02 issue note this is the MINIMAL slice #79 G06/G09/G10 need;
the FULL ops ticket remains #23, and the Pages demo is NOT a product
deployment (雛形 Pages 成功不等於產品部署完成). Existing modules are
REUSED, not forked: lease/expiry semantics from
[watchdog-lease.md](watchdog-lease.md) (#18), quota reservation from
`files_policy.UploadQuotaGate` (#20), secret backup exclusions from
`byok_policy.backup_plan` (#21), ledger totals from #77
`resource_events.summarize()`.

## Alert rules

`AlertRules.evaluate(metrics, clock)` is a pure rule engine over a
metrics dict with an injected clock (missing key = healthy/not
collected; `backup_last_success_at` present with `None` = never backed
up = breach). Every emitted alert carries `rule_id`, `severity`,
`runbook_id`, `count`, `first_seen_at`/`last_seen_at`, `details`.

| Rule id | Fires when | Severity | Runbook |
|---|---|---|---|
| `uptime_probe_failed` | external uptime probe failed ≥ N consecutive (default 3); ≥ 2N escalates | warn → critical | RB-OPS-01 |
| `runner_lease_expiring` | watchdog lease deadline within `lease_expiry_lead_ms` | info | RB-OPS-02 |
| `runner_lease_expired` | `now >= lease_deadline_at` (#18 half-open expiry) | critical | RB-OPS-03 |
| `capacity_high_water` | `disk_used_pct` (or volume reserved vs cap) ≥ `high_water_pct` | warn | RB-OPS-04 |
| `backup_freshness_breach` | last backup success older than RPO (or never) | critical | RB-OPS-05 |
| `cleanup_flush_failed` | cleanup/flush failure count > 0 (#18 `network_failure_policy` domain: block_and_isolate, volumes kept) | warn | RB-OPS-06 |
| `watchdog_failed` | the watchdog ITSELF unreachable/failed (distinct #18 path; nothing derived from watchdog failure) | critical | RB-OPS-07 |

- **Severity ladder**: `info < warn < critical`
  (`SEVERITIES`). A severity ESCALATION breaks the dedup window and
  emits immediately.
- **Dedup**: the same rule re-firing inside `dedup_window_ms` (default
  15 min) is ONE alert with a `count` (visible via `current_alerts()`),
  not N; after the window a new alert with a fresh count is due.
- **Runbook requirement** (每個告警附操作 runbook): every alert carries
  its `runbook_id`; `RunbookRegistry.describe(alert)` returns
  `{runbook_id, owner, steps, escalation}` and
  `RunbookRegistry.assert_complete(AlertRules.RULE_IDS)` is the
  verifier check — the test suite's mutation guard fails if a rule is
  added without a runbook.

## Backup: RPO/RTO and the restore drill

- `BackupPolicy(rpo_ms, rto_ms)`: RPO/RTO are CONFIRMED by a real
  restore drill, not by seeing a dump file (issue acceptance 真實還原
  演練而非僅看到 dump 檔). `freshness(now, last_success)` →
  `{rpo_met, age_ms, breach_alert}`; `last_success=None` (never backed
  up) is a breach — age unknown is not age zero.
- **The drill itself is RUNTIME-BLOCKED** (real off-site Postgres
  restore; see Runtime TODO). What IS delivered:
  `drill_report(snapshot_id)` — the evidence template a completed drill
  must fill — and `validate_drill_record(record)`, which accepts a
  record only when every field of `REQUIRED_DRILL_EVIDENCE` is present
  and non-None: `snapshot_id`, `performed_at`, `sandbox_count`,
  `volumes_restored`, `secret_exclusions`,
  `event_ledger_replay_totals_match`. A record missing evidence is
  INVALID — an evidence-free drill is not a drill.
  - `secret_exclusions` must reflect `byok_policy.backup_plan()` (#21):
    plaintext AND sealed blobs never travel with a backup.
  - `event_ledger_replay_totals_match` must compare against the
    pre-restore `resource_events.summarize()` totals (#77).

## Retention terms (Proposed — product decision pending)

`RetentionPolicy(notice_days=14, volume_retention_days=30,
db_retention_days=180)` — the day values are **Proposed** defaults,
marked for product decision before external users (issue acceptance:
明確 volume 是否備份、免費試用的資料保留條款).

- **DB retention is SEPARATE from volume retention**: workspace/home
  volumes are deleted per the retention term, while DB rows are
  retained as evidence (`retain-db-evidence` action,
  `db_evidence_until`) for the longer db term.
- **Deletion NEVER before its notice window**: volume deletion due =
  exit + retention term, floored at the notice-window close
  (`max(notice_until, exit + retention)`); a retention term shorter
  than the notice is clamped up to it.
- **Explicit exit deletion** (退出刪除, 2026-10-02 note):
  `exit_deletion_requested: true` deletes volumes at the EARLIEST
  lawful moment — the notice-window close, still never before notice.
- `plan_expiry(now, subject)` returns the actions due at `now`:
  `notify` (carries the notice close and the planned deletion date),
  `delete_volume`, `retain-db-evidence`. No DB-deletion action is ever
  planned before the db term.

## Disk admission (high-water)

`DiskAdmission(cap_bytes, high_water_pct=80)`: `admit(nbytes)` denies
when projected use would pass `high_water_pct` of the cap — denial
leaves headroom below the real limit. Atomicity is
`files_policy.UploadQuotaGate.reserve_quota`'s (#20, imported not
forked): one lock around check-then-commit, so N concurrent admissions
can never jointly pass the mark (race-free; tested under threads).

## Data-dir init (idempotent, non-destructive)

`DataDirInit.init_plan(existing)` — issue acceptance 資料目錄初始化可
重跑，不在 setup 腳本中無條件 mkfs；原有資料碟先檢查:

- existing data dir (`has_data`) → `check_and_keep`: fsck + keep,
  `mkfs: false`, EVEN under `force=True` (the no-unconditional-mkfs
  guard; mutation-guarded in tests).
- truly empty / absent → `initialize`; `mkfs: true` only when
  explicitly forced (`force=True`).
- wipe (`destroy_and_initialize`) ONLY with the explicit `destroy`
  flag.
- plans are pure and idempotent: re-running yields the same plan.

## Runtime TODO (blocked; NOT claimed)

- Real off-site Postgres backup + REAL restore drill (fills
  `drill_report` evidence and passes `validate_drill_record`) — RPO/RTO
  stay unconfirmed numbers until then.
- Staging deploy verification + rollback drill that does NOT
  auto-delete existing sandboxes; immutable artifact/image digests
  produced in CI.
- External uptime probes, alert delivery to a reachable channel
  (pager/chat), capacity metrics collection feeding this engine.
- Host disk/cgroup metrics and admission wiring into the real
  control plane (blocked on #11).

## Boundaries

- **#18**: lease expiry semantics, stop-evidence admission and the
  watchdog-itself-failed path are OWNED by watchdog-lease; this slice
  only turns their persisted signals into alerts + runbooks.
- **#20**: the atomic reservation primitive is
  `UploadQuotaGate.reserve_quota`; DiskAdmission adds only the
  high-water projection.
- **#21**: backup secret exclusions are `byok_policy.backup_plan()`;
  no second exclusion list exists here.
- **#77/#25**: ledger totals and billing semantics stay there; the
  drill only compares replay totals.
- **#79**: this minimal slice serves G06/G09/G10 evidence; full ops
  remains #23 (本票).

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
```
