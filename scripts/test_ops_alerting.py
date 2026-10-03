"""Guard tests for the #23 minimal ops slice: alert rules, runbooks,
backup/retention policy, disk admission and data-dir init.

Every rule fires on its metric and stays silent on healthy metrics; the
dedup window collapses repeats into one counted alert; the severity
ladder info < warn < critical holds and escalation breaks dedup; every
rule must carry a runbook (mutation guard fails if one is added without
— 每個告警附操作 runbook); backup freshness breaches past RPO; drill
records missing evidence are invalid; retention never deletes before the
notice window, honors explicit exit deletion (退出刪除) and keeps DB
evidence separate; disk admission denies above the high-water mark
race-free; data-dir init is idempotent and never mkfs's an existing data
dir. Mutation-guard pattern per scripts/test_watchdog_lease.py.
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import ops_alerting as oa  # noqa: E402
from ops_alerting import (  # noqa: E402
    AlertRules, BackupPolicy, DataDirInit, DiskAdmission, RetentionPolicy,
    RunbookRegistry, SEVERITIES, _SEVERITY_RANK)


class FakeClock:
    def __init__(self, now=1_000_000):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now += ms
        return self.now


NOW = 1_000_000


def healthy_metrics(now=NOW):
    return {
        "uptime_probe_consecutive_failures": 0,
        "lease_deadline_at": now + 60_000,
        "disk_used_pct": 50,
        "backup_last_success_at": now - 3_600_000,
        "cleanup_flush_failures": 0,
        "watchdog_reachable": True,
    }


def failing_overlays():
    return {
        oa.RULE_UPTIME: {"uptime_probe_consecutive_failures": 3},
        oa.RULE_LEASE_EXPIRING: {"lease_deadline_at": NOW + 5_000},
        oa.RULE_LEASE_EXPIRED: {"lease_deadline_at": NOW - 1},
        oa.RULE_CAPACITY: {"disk_used_pct": 85},
        oa.RULE_BACKUP: {"backup_last_success_at": NOW - (oa.DAY_MS + 1)},
        oa.RULE_CLEANUP: {"cleanup_flush_failures": 2},
        oa.RULE_WATCHDOG: {"watchdog_reachable": False},
    }


class RuleFiring(unittest.TestCase):
    def test_every_rule_fires_on_its_metric_only(self):
        for rule_id, overlay in failing_overlays().items():
            with self.subTest(rule=rule_id):
                rules = AlertRules()
                alerts = rules.evaluate({**healthy_metrics(), **overlay},
                                        FakeClock(NOW))
                self.assertEqual([a["rule_id"] for a in alerts], [rule_id])

    def test_healthy_metrics_fire_nothing(self):
        self.assertEqual(AlertRules().evaluate(healthy_metrics(),
                                               FakeClock(NOW)), [])
        self.assertEqual(AlertRules().evaluate({}, FakeClock(NOW)),
                         [])  # missing keys are not failures (except backup)

    def test_missing_backup_metric_is_a_breach(self):
        rules = AlertRules()
        alerts = rules.evaluate({**healthy_metrics(),
                                 "backup_last_success_at": None},
                                FakeClock(NOW))
        self.assertEqual([a["rule_id"] for a in alerts],
                         [oa.RULE_BACKUP])  # never backed up = breach

    def test_capacity_rule_accepts_reserved_vs_cap(self):
        rules = AlertRules()
        base = {k: v for k, v in healthy_metrics().items()
                if k != "disk_used_pct"}
        alerts = rules.evaluate({**base, "volume_reserved_bytes": 850,
                                 "volume_cap_bytes": 1000},
                                FakeClock(NOW))
        self.assertEqual([a["rule_id"] for a in alerts], [oa.RULE_CAPACITY])
        self.assertEqual(alerts[0]["details"]["used_pct"], 85)

    def test_watchdog_failed_flag_fires_too(self):
        rules = AlertRules()
        alerts = rules.evaluate({**healthy_metrics(), "watchdog_failed": True,
                                 "watchdog_reachable": True},
                                FakeClock(NOW))
        self.assertEqual([a["rule_id"] for a in alerts], [oa.RULE_WATCHDOG])

    def test_lease_boundary_is_half_open(self):
        rules = AlertRules(config={"lease_expiry_lead_ms": 0})
        clock = FakeClock(NOW)
        metrics = {**healthy_metrics(NOW), "lease_deadline_at": NOW + 1}
        self.assertEqual(rules.evaluate(metrics, clock),
                         [])  # lead 0 and not yet expired -> silent
        clock.advance(1)
        alerts = rules.evaluate(metrics, clock)   # now >= deadline: expired
        self.assertEqual([a["rule_id"] for a in alerts],
                         [oa.RULE_LEASE_EXPIRED])


class DedupWindow(unittest.TestCase):
    def METRIC(self, now=NOW):
        return {**healthy_metrics(now), "disk_used_pct": 85}

    def test_repeats_inside_window_are_one_alert_with_count(self):
        rules, clock = AlertRules(), FakeClock(NOW)
        first = rules.evaluate(self.METRIC(clock.now), clock)
        self.assertEqual(len(first), 1)
        clock.advance(1000)
        self.assertEqual(rules.evaluate(self.METRIC(clock.now), clock), [])
        clock.advance(1000)
        self.assertEqual(rules.evaluate(self.METRIC(clock.now), clock), [])
        current = rules.current_alerts()
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["rule_id"], oa.RULE_CAPACITY)
        self.assertEqual(current[0]["count"], 3)   # 3 fires, 1 alert

    def test_window_elapsed_re_emits_with_fresh_count(self):
        rules, clock = AlertRules(), FakeClock(NOW)
        rules.evaluate(self.METRIC(clock.now), clock)
        clock.advance(rules._cfg["dedup_window_ms"])
        again = rules.evaluate(self.METRIC(clock.now), clock)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["count"], 1)     # new window, new count

    def test_severity_escalation_breaks_dedup(self):
        rules, clock = AlertRules(), FakeClock(NOW)
        warn = rules.evaluate({**healthy_metrics(),
                               "uptime_probe_consecutive_failures": 3},
                              clock)
        self.assertEqual(warn[0]["severity"], "warn")
        clock.advance(1000)   # well inside the dedup window
        critical = rules.evaluate({**healthy_metrics(),
                                   "uptime_probe_consecutive_failures": 6},
                                  clock)
        self.assertEqual([a["severity"] for a in critical], ["critical"])

    def test_distinct_rules_dedup_independently(self):
        rules, clock = AlertRules(), FakeClock(NOW)
        rules.evaluate(self.METRIC(), clock)
        other = rules.evaluate({**healthy_metrics(),
                                "cleanup_flush_failures": 1}, clock)
        self.assertEqual([a["rule_id"] for a in other],
                         [oa.RULE_CLEANUP])   # not suppressed by the other


class SeverityLadder(unittest.TestCase):
    def test_every_alert_severity_is_on_the_ladder(self):
        self.assertEqual(SEVERITIES, ("info", "warn", "critical"))
        for overlay in failing_overlays().values():
            for alert in AlertRules().evaluate(
                    {**healthy_metrics(), **overlay}, FakeClock(NOW)):
                self.assertIn(alert["severity"], SEVERITIES)

    def test_ladder_order_info_warn_critical(self):
        self.assertLess(_SEVERITY_RANK["info"], _SEVERITY_RANK["warn"])
        self.assertLess(_SEVERITY_RANK["warn"], _SEVERITY_RANK["critical"])

    def test_ladder_is_populated_across_rules(self):
        severities = {AlertRules().evaluate(
            {**healthy_metrics(), **overlay}, FakeClock(NOW))[0]["severity"]
            for overlay in failing_overlays().values()}
        self.assertEqual(severities, {"info", "warn", "critical"})

    def test_uptime_escalates_warn_to_critical(self):
        rules, clock = AlertRules(), FakeClock(NOW)
        warn = rules.evaluate({**healthy_metrics(),
                               "uptime_probe_consecutive_failures": 3},
                              clock)[0]
        critical = AlertRules().evaluate(
            {**healthy_metrics(),
             "uptime_probe_consecutive_failures": 6}, clock)[0]
        self.assertEqual((warn["severity"], critical["severity"]),
                         ("warn", "critical"))


class RunbookCoverage(unittest.TestCase):
    def test_every_rule_id_has_a_runbook(self):
        registry = RunbookRegistry()
        registry.assert_complete(AlertRules.RULE_IDS)   # no raise
        for rule_id in AlertRules.RULE_IDS:
            runbook = registry.describe(rule_id)
            for key in ("runbook_id", "owner", "steps", "escalation"):
                self.assertIn(key, runbook)

    def test_every_emitted_alert_carries_its_runbook_id(self):
        registry = RunbookRegistry()
        for overlay in failing_overlays().values():
            alert = AlertRules().evaluate({**healthy_metrics(), **overlay},
                                          FakeClock(NOW))[0]
            self.assertEqual(alert["runbook_id"],
                             registry.describe(alert)["runbook_id"])

    def test_describe_accepts_alert_dicts(self):
        alert = AlertRules().evaluate(
            {**healthy_metrics(),
             "uptime_probe_consecutive_failures": 3}, FakeClock(NOW))[0]
        self.assertEqual(RunbookRegistry().describe(alert)["runbook_id"],
                         alert["runbook_id"])


class BackupFreshness(unittest.TestCase):
    def test_fresh_backup_meets_rpo(self):
        policy = BackupPolicy(rpo_ms=oa.DAY_MS)
        out = policy.freshness(NOW, NOW - 3_600_000)
        self.assertTrue(out["rpo_met"])
        self.assertIsNone(out["breach_alert"])
        self.assertEqual(out["age_ms"], 3_600_000)

    def test_backup_older_than_rpo_breaches_critical(self):
        policy = BackupPolicy(rpo_ms=oa.DAY_MS)
        out = policy.freshness(NOW, NOW - oa.DAY_MS - 1)
        self.assertFalse(out["rpo_met"])
        alert = out["breach_alert"]
        self.assertEqual(alert["rule_id"], oa.RULE_BACKUP)
        self.assertEqual(alert["severity"], "critical")
        self.assertEqual(alert["runbook_id"], "RB-OPS-05")

    def test_never_backed_up_is_a_breach(self):
        out = BackupPolicy().freshness(NOW, None)
        self.assertFalse(out["rpo_met"])
        self.assertIsNotNone(out["breach_alert"])
        self.assertIsNone(out["age_ms"])

    def test_boundary_age_equal_to_rpo_still_meets(self):
        out = BackupPolicy(rpo_ms=1000).freshness(NOW, NOW - 1000)
        self.assertTrue(out["rpo_met"])


class RestoreDrill(unittest.TestCase):
    def test_drill_report_template_lists_required_evidence(self):
        report = BackupPolicy().drill_report("snap-1")
        self.assertEqual(report["status"], "template")
        self.assertEqual(sorted(report["evidence"]),
                         sorted(oa.REQUIRED_DRILL_EVIDENCE))
        self.assertTrue(all(v is None for v in report["evidence"].values()))
        self.assertIn("secret-store/",          # byok backup_plan exclusion
                      report["must_show"]["secret_exclusions"])

    def test_completed_drill_record_with_all_evidence_is_valid(self):
        record = BackupPolicy().drill_report("snap-1")["evidence"]
        record.update({
            "snapshot_id": "snap-1", "performed_at": NOW,
            "sandbox_count": 3, "volumes_restored": ["ws-1", "home-1"],
            "secret_exclusions": oa.backup_plan()["excluded"],
            "event_ledger_replay_totals_match": True,
        })
        self.assertEqual(BackupPolicy().validate_drill_record(record),
                         {"valid": True, "missing": []})

    def test_missing_evidence_field_makes_record_invalid(self):
        record = {"snapshot_id": "snap-1", "performed_at": NOW,
                  "sandbox_count": 3, "volumes_restored": ["ws-1"],
                  "secret_exclusions": oa.backup_plan()["excluded"]}
        # event_ledger_replay_totals_match missing
        result = BackupPolicy().validate_drill_record(record)
        self.assertFalse(result["valid"])
        self.assertEqual(result["missing"],
                         ["event_ledger_replay_totals_match"])

    def test_non_dict_record_is_invalid(self):
        self.assertFalse(BackupPolicy().validate_drill_record(None)["valid"])


def subject(**overrides):
    base = {"workspace_id": "ws-1", "exit_at_ms": 0,
            "exit_deletion_requested": False,
            "volumes": ["workspace", "home"]}
    base.update(overrides)
    return base


def action_types(actions):
    return [a["action"] for a in actions]


class RetentionExpiry(unittest.TestCase):
    def setUp(self):
        self.policy = RetentionPolicy()   # notice 14d, volume 30d, db 180d
        self.day = oa.DAY_MS

    def test_no_deletion_before_notice_window(self):
        for now in (1 * self.day, 13 * self.day, 14 * self.day - 1):
            with self.subTest(now_days=now // self.day):
                actions = self.policy.plan_expiry(now, subject())
                self.assertNotIn("delete_volume", action_types(actions))

    def test_notify_and_db_evidence_start_at_exit(self):
        actions = self.policy.plan_expiry(1 * self.day, subject())
        self.assertEqual(action_types(actions), ["notify",
                                                 "retain-db-evidence"])

    def test_default_term_deletes_after_retention_days(self):
        actions = self.policy.plan_expiry(30 * self.day, subject())
        self.assertIn("delete_volume", action_types(actions))
        delete = next(a for a in actions if a["action"] == "delete_volume")
        self.assertEqual(delete["due_at_ms"], 30 * self.day)
        self.assertEqual(delete["requested"], "retention_term")
        self.assertEqual(delete["volumes"], ["workspace", "home"])

    def test_explicit_exit_deletion_honored_at_notice_close(self):
        actions = self.policy.plan_expiry(
            15 * self.day, subject(exit_deletion_requested=True))
        delete = next(a for a in actions if a["action"] == "delete_volume")
        self.assertEqual(delete["due_at_ms"], 14 * self.day)  # notice close
        self.assertEqual(delete["requested"], "exit_deletion")

    def test_exit_deletion_still_never_before_notice(self):
        self.assertGreaterEqual(
            self.policy._delete_due_at(subject(exit_deletion_requested=True)),
            14 * self.day)

    def test_retention_shorter_than_notice_is_clamped_to_notice(self):
        policy = RetentionPolicy(notice_days=14, volume_retention_days=3)
        self.assertNotIn("delete_volume", action_types(
            policy.plan_expiry(3 * self.day, subject())))
        self.assertIn("delete_volume", action_types(
            policy.plan_expiry(14 * self.day, subject())))

    def test_db_evidence_retained_separately_longer_than_volumes(self):
        actions = self.policy.plan_expiry(30 * self.day, subject())
        db = next(a for a in actions if a["action"] == "retain-db-evidence")
        delete = next(a for a in actions if a["action"] == "delete_volume")
        self.assertGreater(db["retained_until_ms"], delete["due_at_ms"])

    def test_db_evidence_action_expires_at_db_term_but_db_never_deleted_early(self):
        self.assertEqual(action_types(
            self.policy.plan_expiry(181 * self.day, subject())),
            ["notify", "delete_volume"])   # db term passed: no retain action
        for now in (1, 30, 179):
            with self.subTest(now_days=now):
                actions = self.policy.plan_expiry(now * self.day, subject())
                self.assertTrue(set(action_types(actions)) <= {
                    "notify", "delete_volume", "retain-db-evidence"})

    def test_notify_carries_the_deletion_date(self):
        actions = self.policy.plan_expiry(1 * self.day, subject())
        notify = next(a for a in actions if a["action"] == "notify")
        self.assertEqual(notify["volume_deletes_at_ms"], 30 * self.day)
        self.assertEqual(notify["notice_until_ms"], 14 * self.day)


class DiskHighWaterAdmission(unittest.TestCase):
    def test_admits_below_high_water(self):
        disk = DiskAdmission(cap_bytes=1000, high_water_pct=80)
        self.assertTrue(disk.admit(750))

    def test_denies_above_high_water_leaving_headroom(self):
        disk = DiskAdmission(cap_bytes=1000, high_water_pct=80)
        self.assertTrue(disk.admit(750))
        self.assertTrue(disk.admit(50))            # 800 = exactly at mark
        self.assertFalse(disk.admit(1))            # 801 > 800 high-water
        self.assertEqual(disk.projected_use(), 800)   # 200 headroom kept

    def test_boundary_exactly_at_high_water_admits(self):
        disk = DiskAdmission(cap_bytes=1000, high_water_pct=80)
        self.assertTrue(disk.admit(800))     # not > limit

    def test_release_restores_admission_room(self):
        disk = DiskAdmission(cap_bytes=1000, high_water_pct=80)
        disk.admit(800)
        disk.release(300)
        self.assertTrue(disk.admit(250))

    def test_concurrent_admissions_are_race_free(self):
        disk = DiskAdmission(cap_bytes=10_000, high_water_pct=80,
                             stall=lambda: time.sleep(0.001))
        admitted = []

        def try_admit():
            if disk.admit(100):
                admitted.append(100)

        threads = [threading.Thread(target=try_admit) for _ in range(120)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(admitted), 80)      # exactly 8000/100
        self.assertEqual(disk.projected_use(), 8000)

    def test_invalid_config_rejected(self):
        for args in ((0, 80), (-1, 80), (1000, 0), (1000, 100), (1000, -1)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    DiskAdmission(*args)


class DataDirInitRules(unittest.TestCase):
    def test_existing_data_dir_is_check_and_keep_never_mkfs(self):
        existing = {"exists": True, "has_data": True}
        plan = DataDirInit().init_plan(existing)
        self.assertEqual(plan["action"], "check_and_keep")
        self.assertFalse(plan["mkfs"])
        self.assertTrue(plan["check_fs"])
        self.assertFalse(plan["destroy"])

    def test_existing_data_dir_survives_even_explicit_force(self):
        plan = DataDirInit(force=True).init_plan(
            {"exists": True, "has_data": True})
        self.assertEqual(plan["action"], "check_and_keep")
        self.assertFalse(plan["mkfs"])

    def test_init_plan_is_idempotent_on_rerun(self):
        init = DataDirInit(force=True)
        existing = {"exists": True, "has_data": True}
        self.assertEqual(init.init_plan(existing), init.init_plan(existing))
        self.assertTrue(init.init_plan(existing)["idempotent"])

    def test_truly_empty_needs_force_for_mkfs(self):
        for existing in (None, {"exists": True, "has_data": False}):
            with self.subTest(existing=existing):
                self.assertEqual(
                    DataDirInit().init_plan(existing)["mkfs"], False)
                self.assertEqual(
                    DataDirInit(force=True).init_plan(existing)["mkfs"],
                    True)
                self.assertEqual(
                    DataDirInit().init_plan(existing)["action"], "initialize")

    def test_wipe_requires_explicit_destroy_flag(self):
        existing = {"exists": True, "has_data": True}
        for init in (DataDirInit(), DataDirInit(force=True)):
            with self.subTest(force=init.force):
                self.assertFalse(init.init_plan(existing)["destroy"])
        wiped = DataDirInit(destroy=True).init_plan(existing)
        self.assertEqual(wiped["action"], "destroy_and_initialize")
        self.assertTrue(wiped["destroy"])


class MutationGuards(unittest.TestCase):
    """Each safeguard must be load-bearing: break it, the guarantee breaks."""

    def test_runbook_coverage_guard(self):
        registry = RunbookRegistry()
        registry.assert_complete(AlertRules.RULE_IDS)      # guard holds
        incomplete = {rid: runbook for rid, runbook in
                      oa.RUNBOOKS.items() if rid != oa.RULE_WATCHDOG}
        with self.assertRaises(AssertionError):            # bypassed: fails
            RunbookRegistry(incomplete).assert_complete(AlertRules.RULE_IDS)

    def test_dedup_window_guard(self):
        rules, clock = AlertRules(), FakeClock(NOW)
        metric = {**healthy_metrics(), "disk_used_pct": 85}
        self.assertEqual(len(rules.evaluate(metric, clock)), 1)
        self.assertEqual(rules.evaluate(metric, clock), [])  # suppressed
        with mock.patch.object(AlertRules, "_should_emit",
                               lambda self, state, severity, now: True):
            breached = rules.evaluate(metric, clock)
        self.assertEqual(len(breached), 1)   # guard bypassed: alert storm

    def test_no_unconditional_mkfs_guard(self):
        existing = {"exists": True, "has_data": True}
        plan = DataDirInit(force=True).init_plan(existing)
        self.assertEqual(plan["action"], "check_and_keep")  # guard holds
        self.assertFalse(plan["mkfs"])
        with mock.patch.object(DataDirInit, "_existing_data",
                               lambda self, existing: False):
            broken = DataDirInit(force=True).init_plan(existing)
        self.assertEqual(broken["action"], "initialize")  # bypassed
        self.assertTrue(broken["mkfs"])   # unconditional mkfs lands

    def test_retention_notice_guard(self):
        policy = RetentionPolicy()   # notice 14d
        self.assertNotIn("delete_volume", action_types(
            policy.plan_expiry(1 * oa.DAY_MS, subject())))  # guard holds
        with mock.patch.object(RetentionPolicy, "_delete_due_at",
                               lambda self, subj: subj["exit_at_ms"]):
            breached = policy.plan_expiry(1 * oa.DAY_MS, subject())
        self.assertIn("delete_volume", action_types(breached))
        # bypassed: deletion 13 days before the notice window closes

    def test_high_water_guard(self):
        disk = DiskAdmission(cap_bytes=1000, high_water_pct=80)
        disk.admit(750)
        self.assertFalse(disk.admit(100))               # guard holds
        with mock.patch.object(DiskAdmission, "_high_water_limit",
                               lambda self: self.cap_bytes):
            disk.admit(100)                             # bypassed: 850 in
        self.assertEqual(disk.projected_use(), 850)     # headroom gone

    def test_drill_evidence_guard(self):
        incomplete = {"snapshot_id": "snap-1", "performed_at": NOW}
        result = BackupPolicy().validate_drill_record(incomplete)
        self.assertFalse(result["valid"])               # guard holds
        with mock.patch.object(oa, "REQUIRED_DRILL_EVIDENCE", ()):
            breached = BackupPolicy().validate_drill_record(incomplete)
        self.assertTrue(breached["valid"])  # bypassed: evidence-free drill

    def test_backup_freshness_guard(self):
        policy = BackupPolicy(rpo_ms=1000)
        stale = policy.freshness(NOW, NOW - 5000)
        self.assertIsNotNone(stale["breach_alert"])     # guard holds
        with mock.patch.object(BackupPolicy, "_rpo_breach",
                               lambda self, rpo_met: False):
            breached = policy.freshness(NOW, NOW - 5000)
        self.assertIsNone(breached["breach_alert"])  # bypassed: silent RPO


if __name__ == "__main__":
    unittest.main()
