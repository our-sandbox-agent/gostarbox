#!/usr/bin/env python3
"""Minimal ops alerting/backup/retention rules for #23 (stdlib only).

Executable RULES of the #23 acceptance (deploy/backup-restore/minimal ops
alerting), not a runtime: nothing here probes an endpoint, sends an
alert, performs a backup/restore, writes a disk or deletes a volume.
Per the 2026-10-02 issue note this is the MINIMAL slice #79 G06/G09/G10
need (rollback-able deploy + alert rules with runbooks, real-restore/
RPO/retention and exit-deletion terms); the FULL ops ticket remains #23,
and the Pages demo is NOT a product deployment (雛形 Pages 成功不等於
產品部署完成).

Components:
  AlertRules.evaluate(metrics, clock) — pure rule engine over a metrics
      dict with an injected clock; every alert carries a severity from
      the ladder info < warn < critical, a dedup count and a runbook_id
      (每個告警附操作 runbook). The same rule re-firing inside
      dedup_window_ms is ONE alert with a count, not N; a severity
      ESCALATION breaks the dedup window.
  RunbookRegistry — rule id -> {runbook_id, owner, steps, escalation};
      describe(alert) returns the runbook; assert_complete(rule_ids) is
      the verifier check that fails when a rule exists without one.
  BackupPolicy — RPO/RTO targets (config); freshness(now, last_success).
      The REAL restore drill is RUNTIME-BLOCKED (real off-site Postgres
      restore): drill_report(snapshot_id) delivers the evidence template
      and validate_drill_record() checks a completed record carries the
      required evidence (secret exclusions DELEGATED to
      scripts/byok_policy.py backup_plan — reused, not forked).
  RetentionPolicy — trial retention terms (defaults Proposed; product
      decision pending): notice window, workspace/home volume retention
      after exit, explicit exit deletion (退出刪除, 2026-10-02 note), DB
      evidence retention SEPARATE from volume retention. plan_expiry
      NEVER plans a deletion before its notice window.
  DiskAdmission — projected-use admission against a high-water mark
      (denial leaves headroom below the cap); wraps files_policy
      UploadQuotaGate.reserve_quota (imported, not forked) so N
      concurrent admissions are race-free.
  DataDirInit — idempotent, non-destructive data-dir init rules: an
      existing data dir is check-fs-and-keep (NEVER unconditional mkfs —
      issue acceptance 原有資料碟先檢查); mkfs only when truly empty and
      explicitly forced; a wipe requires the explicit destroy flag.

metrics keys read by AlertRules (a missing key = healthy/unknown):
  uptime_probe_consecutive_failures  int   consecutive external probe failures
  lease_deadline_at                  ms    watchdog lease deadline (#18
                                           half-open expiry: expired once
                                           now >= deadline_at)
  disk_used_pct                      0-100 disk usage percent
  volume_reserved_bytes + volume_cap_bytes   reserved-vs-cap alternative
  backup_last_success_at             ms    last successful backup; the KEY
                                           present with None = never backed
                                           up = breach; key absent = not
                                           collected (silent)
  cleanup_flush_failures             int   cleanup/flush failures (#18
                                           network_failure_policy domain)
  watchdog_reachable / watchdog_failed       the watchdog ITSELF (#18
                                           watchdog_unreachable_alert)

Tests: scripts/test_ops_alerting.py (mutate-and-fail guards included).
Spec: docs/contracts/ops-alerting.md.
"""
from byok_policy import backup_plan
from files_policy import UploadQuotaGate

DAY_MS = 24 * 60 * 60 * 1000

SEVERITIES = ("info", "warn", "critical")
_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}

RULE_UPTIME = "uptime_probe_failed"
RULE_LEASE_EXPIRING = "runner_lease_expiring"
RULE_LEASE_EXPIRED = "runner_lease_expired"
RULE_CAPACITY = "capacity_high_water"
RULE_BACKUP = "backup_freshness_breach"
RULE_CLEANUP = "cleanup_flush_failed"
RULE_WATCHDOG = "watchdog_failed"

# 每個告警附操作 runbook: every rule id below MUST have an entry here —
# RunbookRegistry.assert_complete is the verifier check, and the test
# suite's mutation guard fails if a rule is added without one.
RUNBOOKS = {
    RULE_UPTIME: {
        "runbook_id": "RB-OPS-01",
        "owner": "on-call",
        "steps": [
            "confirm the probe target/region from alert details; the Pages "
            "demo is NOT the product deployment",
            "check product ingress + last deploy health; prepare rollback "
            "of the last version if the deploy is the suspect",
            "ack and post status; resolve when the probe is green again",
        ],
        "escalation": "page secondary on-call after 15 min unacknowledged",
    },
    RULE_LEASE_EXPIRING: {
        "runbook_id": "RB-OPS-02",
        "owner": "sandbox-runtime",
        "steps": [
            "check runner heartbeat path latency and host watchdog "
            "reachability",
            "if the deadline still passes, expect the #18 fence + Lost "
            "handling; no manual renew of an old epoch",
        ],
        "escalation": "open sandbox-runtime ticket if recurring",
    },
    RULE_LEASE_EXPIRED: {
        "runbook_id": "RB-OPS-03",
        "owner": "sandbox-runtime",
        "steps": [
            "confirm expiry per watchdog semantics (half-open: expired once "
            "now >= deadline_at)",
            "fence the old epoch; collect stop evidence — a dead runner's "
            "own report is NOT proof (#18)",
            "billing cutoff and capacity release ONLY at confirmed stop",
        ],
        "escalation": "page sandbox-runtime if unconfirmed past "
                      "confirm_timeout_ms (stop_unconfirmed_escalation)",
    },
    RULE_CAPACITY: {
        "runbook_id": "RB-OPS-04",
        "owner": "platform",
        "steps": [
            "check df/df -i per volume against the high-water mark",
            "relieve by refusing admissions (DiskAdmission) and cleaning "
            "expired snapshots — never by deleting user data",
            "grow the volume or shed load; recheck the watermark",
        ],
        "escalation": "page platform if the cap (not just high-water) is hit",
    },
    RULE_BACKUP: {
        "runbook_id": "RB-OPS-05",
        "owner": "data",
        "steps": [
            "check the backup job logs and off-site target reachability",
            "treat everything since last_success as an at-risk window "
            "(RPO breach); kick a manual backup now",
            "after recovery, schedule a restore drill and record it via "
            "BackupPolicy.validate_drill_record",
        ],
        "escalation": "page data owner immediately — RPO breach is "
                      "data-loss exposure",
    },
    RULE_CLEANUP: {
        "runbook_id": "RB-OPS-06",
        "owner": "sandbox-runtime",
        "steps": [
            "network cleanup/flush failure: block_and_isolate per #18 "
            "network_failure_policy — volumes KEPT",
            "never trigger a data-deletion destroy from a cleanup failure",
            "human decision on the isolated sandbox's disposition",
        ],
        "escalation": "open sandbox-runtime ticket; requires_human by "
                      "default",
    },
    RULE_WATCHDOG: {
        "runbook_id": "RB-OPS-07",
        "owner": "platform",
        "steps": [
            "the WATCHDOG ITSELF is unreachable: display Unknown; never "
            "derive stop confirmation, billing cutoff or capacity release "
            "from watchdog failure (#18)",
            "restart/restore the watchdog; audit lease states after "
            "recovery",
        ],
        "escalation": "page platform immediately",
    },
}


class RunbookRegistry:
    """rule id -> runbook {runbook_id, owner, steps, escalation}.

    describe(alert) accepts an alert dict or a bare rule id. The named
    helpers are the verifier checks, not extension points.
    """

    def __init__(self, runbooks=None):
        self._runbooks = RUNBOOKS if runbooks is None else runbooks

    def describe(self, alert):
        rule_id = alert["rule_id"] if isinstance(alert, dict) else alert
        runbook = self._runbooks.get(rule_id)
        if runbook is None:
            raise KeyError(f"no runbook for rule {rule_id!r}")
        return runbook

    def missing_for(self, rule_ids):
        return sorted(rule_id for rule_id in rule_ids
                      if rule_id not in self._runbooks)

    def assert_complete(self, rule_ids):
        """Verifier check: every rule id has a runbook (每個告警附操作
        runbook). Raises AssertionError naming the uncovered rules."""
        missing = self.missing_for(rule_ids)
        if missing:
            raise AssertionError(f"rules without runbooks: {missing}")


# ------------------------------------------------------------------ backup

# What a REAL restore drill record must carry (the drill itself is
# runtime-blocked; a completed record without these is not evidence).
REQUIRED_DRILL_EVIDENCE = (
    "snapshot_id", "performed_at", "sandbox_count", "volumes_restored",
    "secret_exclusions", "event_ledger_replay_totals_match",
)


class BackupPolicy:
    """RPO/RTO targets + backup freshness + the restore-drill contract.

    RPO/RTO are CONFIRMED by a real restore drill, not by seeing a dump
    file (issue acceptance); the drill is runtime-blocked (real off-site
    Postgres restore, #23 runtime TODO), so this class delivers the
    template and validates completed records.
    """

    def __init__(self, rpo_ms=DAY_MS, rto_ms=4 * 60 * 60 * 1000):
        for name, value in (("rpo_ms", rpo_ms), ("rto_ms", rto_ms)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive int, got {value!r}")
        self.rpo_ms = rpo_ms
        self.rto_ms = rto_ms

    def freshness(self, now_ms, last_success_ms):
        """Backup freshness vs the RPO target.

        last_success_ms None (never backed up) is a breach: age unknown is
        not age zero. Returns {rpo_met, age_ms, breach_alert}; a breach
        carries the backup_freshness_breach alert vocabulary (critical,
        runbook included) so AlertRules adopts it without a second table.
        """
        if last_success_ms is None:
            age_ms, rpo_met = None, False
        else:
            age_ms = now_ms - last_success_ms
            rpo_met = age_ms <= self.rpo_ms
        breach = self._rpo_breach(rpo_met)
        return {
            "rpo_met": rpo_met,
            "age_ms": age_ms,
            "breach_alert": ({
                "rule_id": RULE_BACKUP,
                "severity": "critical",
                "runbook_id": RUNBOOKS[RULE_BACKUP]["runbook_id"],
            } if breach else None),
        }

    def _rpo_breach(self, rpo_met):
        """Mutation point (guard test): rpo not met IS the breach."""
        return not rpo_met

    def drill_report(self, snapshot_id):
        """Template of what a REAL restore drill must show (runtime-blocked;
        delivered per the #23 acceptance: 真實還原演練，而非僅看到 dump 檔).

        A completed drill fills evidence[] with non-None values and must
        pass validate_drill_record. Secret exclusions come from the #21
        byok backup_plan (plaintext AND sealed blobs never travel with a
        backup); ledger totals must match the pre-restore
        resource_events.summarize() projection.
        """
        return {
            "report_type": "restore-drill",
            "snapshot_id": snapshot_id,
            "status": "template",
            "rpo_ms": self.rpo_ms,
            "rto_ms": self.rto_ms,
            "evidence": {field: None for field in REQUIRED_DRILL_EVIDENCE},
            "required_evidence": list(REQUIRED_DRILL_EVIDENCE),
            "must_show": {
                "sandbox_count": "restored sandbox count equals the "
                                 "snapshot's control-plane state",
                "volumes_restored": "every workspace/home volume in the "
                                    "snapshot is restored and mountable",
                "secret_exclusions": f"backup EXCLUDES {backup_plan()['excluded']} "
                                     "(byok backup_plan)",
                "event_ledger_replay_totals_match": "ledger replay totals "
                                                    "equal pre-restore summarize() totals",
                "rto_observed_ms": "wall-clock restore duration vs rto_ms",
            },
            "note": ("the drill itself is RUNTIME-BLOCKED (real off-site "
                     "restore, #23); a completed record fills evidence[] "
                     "and must pass validate_drill_record"),
        }

    def validate_drill_record(self, record):
        """A completed drill record is evidence ONLY if every required
        field is present and non-None (missing evidence -> invalid)."""
        missing = [field for field in REQUIRED_DRILL_EVIDENCE
                   if not isinstance(record, dict) or record.get(field) is None]
        return {"valid": not missing, "missing": missing}


# --------------------------------------------------------------- retention

class RetentionPolicy:
    """Trial data retention terms (defaults Proposed — product decision).

    Volume retention and DB retention are SEPARATE: workspace/home
    volumes are deleted after the retention term, while DB rows are
    retained as evidence (billing/audit) for the longer db term. Explicit
    exit deletion (退出刪除, 2026-10-02 note) deletes volumes at the
    EARLIEST lawful moment — still never before the notice window.

    subject: {"workspace_id": str, "exit_at_ms": int,
              "exit_deletion_requested": bool,
              "volumes": ["workspace", "home"]}
    """

    def __init__(self, notice_days=14, volume_retention_days=30,
                 db_retention_days=180):
        for name, value in (("notice_days", notice_days),
                            ("volume_retention_days", volume_retention_days),
                            ("db_retention_days", db_retention_days)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive int, got {value!r}")
        self.notice_days = notice_days
        self.volume_retention_days = volume_retention_days
        self.db_retention_days = db_retention_days

    def plan_expiry(self, now_ms, subject):
        """Actions due at now_ms: notify | delete_volume |
        retain-db-evidence. Deletion is NEVER planned before the notice
        window closes (the guard lives in _delete_due_at)."""
        exit_at = subject["exit_at_ms"]
        notice_until = exit_at + self.notice_days * DAY_MS
        delete_at = self._delete_due_at(subject)
        db_until = self.db_evidence_until(subject)
        actions = []
        if now_ms >= exit_at:
            actions.append({
                "action": "notify",
                "target": subject.get("workspace_id"),
                "due_at_ms": exit_at,
                "notice_until_ms": notice_until,
                "volume_deletes_at_ms": delete_at,
            })
        if now_ms >= delete_at:
            actions.append({
                "action": "delete_volume",
                "target": subject.get("workspace_id"),
                "due_at_ms": delete_at,
                "volumes": list(subject.get("volumes", ("workspace", "home"))),
                "requested": ("exit_deletion"
                              if subject.get("exit_deletion_requested")
                              else "retention_term"),
            })
        if exit_at <= now_ms < db_until:
            actions.append({
                "action": "retain-db-evidence",
                "target": subject.get("workspace_id"),
                "due_at_ms": exit_at,
                "retained_until_ms": db_until,
            })
        return actions

    def _delete_due_at(self, subject):
        """Mutation point (guard test): the notice-window floor. Volume
        deletion due = exit + retention term, but NEVER before the notice
        window closes — explicit exit deletion lands exactly at the
        notice close (earliest lawful moment), not at exit."""
        exit_at = subject["exit_at_ms"]
        notice_until = exit_at + self.notice_days * DAY_MS
        if subject.get("exit_deletion_requested"):
            return notice_until
        return max(notice_until,
                   exit_at + self.volume_retention_days * DAY_MS)

    def db_evidence_until(self, subject):
        """DB rows outlive the volumes (evidence/billing), separately."""
        return subject["exit_at_ms"] + self.db_retention_days * DAY_MS


# ---------------------------------------------------------- disk admission

class DiskAdmission:
    """Projected-use admission against a high-water mark.

    admit(nbytes) denies when projected use would pass high_water_pct of
    the cap — denial leaves headroom below the real limit. Atomicity is
    files_policy UploadQuotaGate.reserve_quota's (#20): one lock around
    check-then-commit, so N concurrent admissions can never jointly pass
    the mark. Reused, not forked.
    """

    def __init__(self, cap_bytes, high_water_pct=80, stall=None):
        if type(cap_bytes) is not int or cap_bytes <= 0:
            raise ValueError("cap_bytes must be a positive int")
        if not (0 < high_water_pct < 100):
            raise ValueError("high_water_pct must be in (0, 100)")
        self.cap_bytes = cap_bytes
        self.high_water_pct = high_water_pct
        self._gate = UploadQuotaGate(self._high_water_limit(), stall=stall)

    def _high_water_limit(self):
        """Mutation point (guard test): the headroom floor — the admission
        ceiling is the high-water byte limit, NOT the cap."""
        return self.cap_bytes * self.high_water_pct // 100

    def admit(self, nbytes):
        """Atomic projected-use admission; False = over high-water.

        The ceiling is refreshed from _high_water_limit so the high-water
        calc is the single source (config is fixed at init; the refresh
        is idempotent, and the gate's own lock still guards the
        check-then-commit)."""
        self._gate.limit = self._high_water_limit()
        return self._gate.reserve_quota(nbytes)

    def release(self, released_bytes):
        self._gate.release(released_bytes)

    def projected_use(self):
        return self._gate.reserved()


# ------------------------------------------------------------- data dir init

class DataDirInit:
    """Idempotent, non-destructive data-dir init rules (#23 acceptance:
    資料目錄初始化可重跑，不在 setup 腳本中無條件 mkfs；原有資料碟先檢查).

    existing: None (no data dir yet) or {"exists": bool, "has_data": bool}.
    An existing data dir is check-fs-and-keep EVEN under force; mkfs is
    planned only when truly empty AND explicitly forced; a wipe needs the
    explicit destroy flag.
    """

    def __init__(self, force=False, destroy=False):
        self.force = force
        self.destroy = destroy

    def init_plan(self, existing):
        if self.destroy:
            return self._plan("destroy_and_initialize", mkfs=True,
                              destroy=True)
        if self._existing_data(existing):
            return self._plan("check_and_keep", mkfs=False, destroy=False,
                              check_fs=True)
        return self._plan("initialize", mkfs=bool(self.force), destroy=False)

    def _existing_data(self, existing):
        """Mutation point (guard test): an existing data dir with data is
        NEVER re-initialized — this is the no-unconditional-mkfs guard."""
        return bool(existing) and bool(existing.get("has_data"))

    @staticmethod
    def _plan(action, mkfs, destroy, check_fs=False):
        return {"action": action, "mkfs": mkfs, "destroy": destroy,
                "check_fs": check_fs, "idempotent": True}


# ------------------------------------------------------------ alert engine

class AlertRules:
    """Pure rule engine over a metrics dict with an injected clock.

    evaluate(metrics, clock) returns the alerts DUE for sending: a first
    fire, a fire after dedup_window_ms, or a severity ESCALATION. Repeats
    inside the window update the dedup count (current_alerts) and are
    NOT re-sent. Every alert carries rule_id, severity
    (info/warn/critical), runbook_id, count, first/last_seen and details.

    The named _-prefixed helpers are deliberate mutation points for the
    guard tests (scripts/test_ops_alerting.py), not extension points.
    """

    RULE_IDS = frozenset((RULE_UPTIME, RULE_LEASE_EXPIRING, RULE_LEASE_EXPIRED,
                          RULE_CAPACITY, RULE_BACKUP, RULE_CLEANUP,
                          RULE_WATCHDOG))

    DEFAULTS = {
        "uptime_consecutive_failures": 3,   # N consecutive probe failures
        "uptime_critical_multiplier": 2,    # >= 2N consecutive -> critical
        "lease_expiry_lead_ms": 10_000,     # expiring = deadline within lead
        "high_water_pct": 80,
        "rpo_ms": DAY_MS,                   # backup freshness target
        "dedup_window_ms": 15 * 60 * 1000,
    }

    def __init__(self, config=None):
        self._cfg = dict(self.DEFAULTS)
        self._cfg.update(config or {})
        self._backup = BackupPolicy(rpo_ms=self._cfg["rpo_ms"])
        self._state = {}   # rule_id -> dedup state

    def evaluate(self, metrics, clock):
        now = clock()
        emitted = []
        for rule_id, severity, details in self._fires(metrics, now):
            state = self._register_fire(rule_id, now)
            if self._should_emit(state, severity, now):
                state["last_emitted_at"] = now
                state["severity"] = severity
                emitted.append({
                    "rule_id": rule_id,
                    "severity": severity,
                    "runbook_id": RUNBOOKS[rule_id]["runbook_id"],
                    "count": state["count"],
                    "first_seen_at": state["first_seen_at"],
                    "last_seen_at": state["last_seen_at"],
                    "details": details,
                })
        return emitted

    def current_alerts(self):
        """Dedup view: one entry per fired rule with its window count."""
        return [dict(state, rule_id=rule_id)
                for rule_id, state in self._state.items()]

    # ------------------------------------------------------------ rules
    def _fires(self, metrics, now):
        """All rules firing over the metrics snapshot: (rule_id,
        severity, details) triples, order-stable."""
        fires = []

        failures = metrics.get("uptime_probe_consecutive_failures", 0)
        threshold = self._cfg["uptime_consecutive_failures"]
        if failures >= threshold:
            severity = ("critical"
                        if failures >= threshold * self._cfg[
                            "uptime_critical_multiplier"] else "warn")
            fires.append((RULE_UPTIME, severity,
                          {"consecutive_failures": failures,
                           "threshold": threshold}))

        deadline = metrics.get("lease_deadline_at")
        if deadline is not None:
            remaining = deadline - now
            if remaining <= 0:   # #18 half-open: expired once now >= deadline
                fires.append((RULE_LEASE_EXPIRED, "critical",
                              {"deadline_at": deadline, "now": now}))
            elif remaining <= self._cfg["lease_expiry_lead_ms"]:
                fires.append((RULE_LEASE_EXPIRING, "info",
                              {"deadline_at": deadline,
                               "remaining_ms": remaining}))

        used_pct = self._used_pct(metrics)
        if used_pct is not None and used_pct >= self._cfg["high_water_pct"]:
            fires.append((RULE_CAPACITY, "warn",
                          {"used_pct": used_pct,
                           "high_water_pct": self._cfg["high_water_pct"]}))

        if "backup_last_success_at" in metrics:
            freshness = self._backup.freshness(
                now, metrics["backup_last_success_at"])
            if freshness["breach_alert"] is not None:
                fires.append((RULE_BACKUP, "critical",
                              {"age_ms": freshness["age_ms"],
                               "rpo_ms": self._backup.rpo_ms}))

        cleanup_failures = metrics.get("cleanup_flush_failures", 0)
        if cleanup_failures > 0:
            fires.append((RULE_CLEANUP, "warn",
                          {"failures": cleanup_failures,
                           "isolation": "block_and_isolate"}))  # #18 policy

        if metrics.get("watchdog_reachable") is False \
                or metrics.get("watchdog_failed"):
            fires.append((RULE_WATCHDOG, "critical",
                          {"flag": "watchdog_failed"}))  # distinct, #18
        return fires

    @staticmethod
    def _used_pct(metrics):
        """disk_used_pct wins; otherwise derive from reserved vs cap."""
        pct = metrics.get("disk_used_pct")
        if pct is not None:
            return pct
        reserved = metrics.get("volume_reserved_bytes")
        cap = metrics.get("volume_cap_bytes")
        if reserved is None or not cap:
            return None
        return reserved * 100 // cap

    # ------------------------------------------------------------ dedup
    def _register_fire(self, rule_id, now):
        state = self._state.get(rule_id)
        if state is None:
            state = {"count": 0, "first_seen_at": now, "last_seen_at": now,
                     "last_emitted_at": None, "severity": None}
            self._state[rule_id] = state
        if (state["last_emitted_at"] is not None
                and now - state["last_emitted_at"]
                >= self._cfg["dedup_window_ms"]):
            state["count"] = 0           # dedup window elapsed: new window
            state["first_seen_at"] = now
        state["count"] += 1
        state["last_seen_at"] = now
        return state

    def _should_emit(self, state, severity, now):
        """Mutation point (guard test): the dedup window. Emit on first
        fire, after dedup_window_ms (count already reset by
        _register_fire), or on a severity ESCALATION (rank on the
        SEVERITIES ladder); suppress repeats inside the window."""
        if state["last_emitted_at"] is None:
            return True
        if now - state["last_emitted_at"] >= self._cfg["dedup_window_ms"]:
            return True
        if _SEVERITY_RANK[severity] > _SEVERITY_RANK[state["severity"]]:
            return True
        return False
