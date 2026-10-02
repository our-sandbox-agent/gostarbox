"""Replayable lifecycle fixtures + targeted #25 semantics tests.

Part 1 replays every case in docs/contracts/resource-lifecycle-fixtures.json
through the #77 EventLedger and asserts exact hand-computed totals (the
ledger-examples idiom, extended to full event fixtures).

Part 2 targets the new semantics directly: trash/purge independence from
sandbox resume/destroy, snapshot delete-confirmation (TTL expiry is a request),
the sealed-period no-restatement rule, and the snapshot placeholder inventory.

Part 3 mutation guards prove each new safeguard is load-bearing (pattern per
scripts/test_resource_events.py).
"""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import resource_events  # noqa: E402
from resource_events import EventLedger  # noqa: E402

FIXTURES = json.loads(
    (Path(__file__).resolve().parents[1]
     / "docs/contracts/resource-lifecycle-fixtures.json").read_text())


def by_name(name):
    return next(case for case in FIXTURES if case["name"] == name)


def envelope(case_name, index, compact, prefix=""):
    payload = dict(compact.get("payload") or {})
    if compact.get("quantity") is not None:
        payload["quantity"] = compact["quantity"]
    return {
        "schema_version": 1,
        "event_id": f"{case_name}-{prefix}{index}",
        "tenant_id": "t1",
        "workspace_id": "ws1",
        "sandbox_id": "sb1",
        "resource_type": compact["resource_type"],
        "resource_id": compact["resource_id"],
        "source_id": "runner-1",
        "generation": compact.get("generation", 1),
        "source_seq": index + 1,
        "effective_at_ms": compact["effective_at_ms"],
        "recorded_at_ms": compact.get("recorded_at_ms", compact["effective_at_ms"]),
        "type": compact["type"],
        "reason": "fixture",
        "payload": payload,
        "certainty": "confirmed",
    }


def run_fixture(case):
    """Expand compact fixture events to full envelopes and replay them."""
    events = [envelope(case["name"], i, c) for i, c in enumerate(case["events"])]
    ledger = EventLedger()
    for ev in events:
        assert ledger.append(ev)["status"] == "appended"
    corrections = [envelope(case["name"], len(events) + i, c, prefix="c")
                   for i, c in enumerate(case.get("corrections", []))]
    receipts = ledger.apply_correction(corrections, case.get("sealed_periods", ()))
    return ledger, events, receipts


def full_expected(expected):
    """Fill per-row defaults so comparison against summarize() is exact."""
    rows = {}
    for rkey, row in expected.items():
        rtype, rid = rkey.split("/", 1)
        rows[rkey] = {
            "resource_type": rtype,
            "resource_id": rid,
            "confirmed_quantity_ms": row["confirmed_quantity_ms"],
            "uncertain_ms": row.get("uncertain_ms", 0),
            "uncertain_capacity_ranges": row.get("uncertain_capacity_ranges", []),
            "open_capacity": row.get("open_capacity"),
        }
    return rows


def check_case(case, ledger, receipts):
    assert ledger.summarize() == full_expected(case["expected"]), ledger.summarize()
    for got, want in zip(receipts, case.get("expected_correction_receipts", [])):
        assert {k: v for k, v in got.items() if k in want} == want, (got, want)
    assert len(receipts) == len(case.get("expected_correction_receipts",
                                         case.get("corrections", [])))
    for label, bounds in case.get("windows", {}).items():
        window_totals = {rkey: row["confirmed_quantity_ms"]
                         for rkey, row in ledger.summarize(window=bounds).items()}
        expected = {rkey: row["confirmed_quantity_ms"]
                    for rkey, row in case["expected_window_totals"][label].items()}
        assert window_totals == expected, (label, window_totals)


class FixtureReplay(unittest.TestCase):
    """Each fixture replays through EventLedger to its exact hand-computed
    totals; sealed corrections flag manual; window totals hold."""

    def test_all_fixtures_replay_to_expected_totals(self):
        for case in FIXTURES:
            with self.subTest(case=case["name"]):
                ledger, events, receipts = run_fixture(case)
                check_case(case, ledger, receipts)
                flagged = [r for r in receipts if r["status"] == "flagged_manual"]
                if flagged:
                    # flagged corrections are never stored in event history
                    self.assertEqual(len(ledger.events()), len(events))
                    self.assertTrue(any("sealed" in f["reason"]
                                        for f in ledger.flags()))

    def test_full_redelivery_does_not_double_count(self):
        for case in FIXTURES:
            with self.subTest(case=case["name"]):
                ledger, events, _ = run_fixture(case)
                before = ledger.summarize()
                for ev in events:  # the whole batch arrives twice
                    self.assertEqual(ledger.append(ev)["status"], "duplicate")
                self.assertEqual(ledger.summarize(), before)

    def test_fixture_roundtrip_through_persistence(self):
        for case in FIXTURES:
            with self.subTest(case=case["name"]):
                ledger, _, _ = run_fixture(case)
                revived = EventLedger.from_json(ledger.to_json())
                self.assertEqual(revived.summarize(), ledger.summarize())


class TrashPurgeIndependence(unittest.TestCase):
    """volume.trashed starts a retention window but ends nothing; only
    volume.purged ends accrual, independent of sandbox resume/destroy."""

    def test_trash_does_not_end_accrual(self):
        ledger = EventLedger()
        ledger.append(envelope("t", 0, {"type": "resource.provisioned",
                                        "resource_type": "workspace_volume",
                                        "resource_id": "v1", "effective_at_ms": 0,
                                        "quantity": {"volume_provisioned": 1024}}))
        ledger.append(envelope("t", 1, {"type": "volume.trashed",
                                        "resource_type": "workspace_volume",
                                        "resource_id": "v1", "effective_at_ms": 1000,
                                        "payload": {"retention_deadline_ms": 4000}}))
        row = ledger.summarize()["workspace_volume/v1"]
        self.assertEqual(row["confirmed_quantity_ms"], {})  # still open, accruing
        self.assertEqual(row["open_capacity"],
                         {"start_ms": 0, "quantity": {"volume_provisioned": 1024}})

    def test_purge_after_sandbox_destroy_still_meters_retention(self):
        # sandbox destroy is NOT a volume release: the trashed volume keeps
        # metering through retention until its own confirmed purge
        ledger = EventLedger()
        ledger.append(envelope("t", 0, {"type": "runtime.started",
                                        "resource_type": "runtime",
                                        "resource_id": "r1", "effective_at_ms": 0,
                                        "quantity": {"cpu_reserved": 1000}}))
        ledger.append(envelope("t", 1, {"type": "resource.provisioned",
                                        "resource_type": "workspace_volume",
                                        "resource_id": "v1", "effective_at_ms": 0,
                                        "quantity": {"volume_provisioned": 1024}}))
        ledger.append(envelope("t", 2, {"type": "runtime.stopped",
                                        "resource_type": "runtime",
                                        "resource_id": "r1",
                                        "effective_at_ms": 2000}))  # destroy
        ledger.append(envelope("t", 3, {"type": "volume.trashed",
                                        "resource_type": "workspace_volume",
                                        "resource_id": "v1", "effective_at_ms": 2000,
                                        "payload": {"retention_deadline_ms": 5000}}))
        ledger.append(envelope("t", 4, {"type": "volume.purged",
                                        "resource_type": "workspace_volume",
                                        "resource_id": "v1",
                                        "effective_at_ms": 5000}))
        totals = ledger.summarize()
        self.assertEqual(totals["workspace_volume/v1"]["confirmed_quantity_ms"],
                         {"volume_provisioned": 1024 * 5000})  # not 1024 * 2000
        self.assertEqual(totals["runtime/r1"]["confirmed_quantity_ms"],
                         {"cpu_reserved": 1000 * 2000})

    def test_resume_cycle_leaves_trashed_volume_accruing(self):
        # trash->purge also ignores sandbox resume: a resumed runtime (new
        # generation) never reopens or closes the volume interval
        ledger = EventLedger()
        ledger.append(envelope("t", 0, {"type": "resource.provisioned",
                                        "resource_type": "home_volume",
                                        "resource_id": "h1", "effective_at_ms": 0,
                                        "quantity": {"volume_provisioned": 512}}))
        ledger.append(envelope("t", 1, {"type": "volume.trashed",
                                        "resource_type": "home_volume",
                                        "resource_id": "h1", "effective_at_ms": 1000,
                                        "payload": {"retention_deadline_ms": 4000}}))
        ledger.append(envelope("t", 2, {"type": "runtime.started",
                                        "resource_type": "runtime",
                                        "resource_id": "r2", "effective_at_ms": 2000,
                                        "generation": 2,
                                        "quantity": {"cpu_reserved": 1000}}))
        ledger.append(envelope("t", 3, {"type": "volume.purged",
                                        "resource_type": "home_volume",
                                        "resource_id": "h1",
                                        "effective_at_ms": 4000}))
        row = ledger.summarize()["home_volume/h1"]
        self.assertEqual(row["confirmed_quantity_ms"],
                         {"volume_provisioned": 512 * 4000})  # straight through resume


class SnapshotDeleteConfirmation(unittest.TestCase):
    """snapshot.expired is a delete REQUEST; accrual ends only at the
    confirmed snapshot.deleted."""

    def test_expired_without_confirmed_delete_keeps_accruing(self):
        ledger = EventLedger()
        ledger.append(envelope("t", 0, {"type": "snapshot.created",
                                        "resource_type": "snapshot",
                                        "resource_id": "s1", "effective_at_ms": 0,
                                        "quantity": {"snapshot_stored": 2048}}))
        ledger.append(envelope("t", 1, {"type": "snapshot.expired",
                                        "resource_type": "snapshot",
                                        "resource_id": "s1", "effective_at_ms": 2000,
                                        "payload": {"retention_deadline_ms": 2000}}))
        row = ledger.summarize()["snapshot/s1"]
        self.assertEqual(row["confirmed_quantity_ms"], {})  # open, still accruing
        self.assertEqual(row["open_capacity"],
                         {"start_ms": 0, "quantity": {"snapshot_stored": 2048}})

    def test_snapshot_inventory_is_empty_placeholder(self):
        # placeholder contract: no real snapshot ops exist, so the live
        # inventory is {} even when metering events are in the ledger —
        # usage is never fabricated from the placeholder
        ledger = EventLedger()
        ledger.append(envelope("t", 0, {"type": "snapshot.created",
                                        "resource_type": "snapshot",
                                        "resource_id": "s1", "effective_at_ms": 0,
                                        "quantity": {"snapshot_stored": 2048}}))
        self.assertEqual(ledger.snapshot_inventory(), {})
        self.assertEqual(EventLedger().snapshot_inventory(), {})

    def test_snapshot_ttl_and_release_both_recorded(self):
        ledger = EventLedger()
        ledger.append(envelope("t", 0, {"type": "snapshot.created",
                                        "resource_type": "snapshot",
                                        "resource_id": "s1", "effective_at_ms": 0,
                                        "quantity": {"snapshot_stored": 2048}}))
        expiry = envelope("t", 1, {"type": "snapshot.expired",
                                   "resource_type": "snapshot",
                                   "resource_id": "s1", "effective_at_ms": 2000,
                                   "payload": {"retention_deadline_ms": 2000}})
        release = envelope("t", 2, {"type": "snapshot.deleted",
                                    "resource_type": "snapshot",
                                    "resource_id": "s1", "effective_at_ms": 6500})
        ledger.append(expiry)
        ledger.append(release)
        stored = {ev["type"]: ev for ev in ledger.events("snapshot/s1")}
        self.assertIn("retention_deadline_ms", stored["snapshot.expired"]["payload"])
        self.assertEqual(stored["snapshot.deleted"]["effective_at_ms"], 6500)


class SealedPeriodNoRestatement(unittest.TestCase):
    """Corrections intersecting a sealed billing period are flagged for
    manual handling, never auto-applied (不回溯改已封帳數量)."""

    SEALED = ({"period": "2026-08", "start_ms": 0, "end_ms": 4000},)

    def _lost_ledger(self):
        ledger, _, _ = run_fixture(by_name("lost-correction-in-sealed-month"))
        return ledger

    def test_correction_inside_sealed_flagged_and_totals_unchanged(self):
        ledger = self._lost_ledger()
        before = ledger.summarize(window=(0, 4000))
        late = envelope("manual", 100, {"type": "runtime.stopped",
                                      "resource_type": "runtime",
                                      "resource_id": "r1",
                                      "effective_at_ms": 2000,
                                      "recorded_at_ms": 5000})
        receipts = ledger.apply_correction([late], self.SEALED)
        self.assertEqual(receipts[0]["status"], "flagged_manual")
        self.assertEqual(receipts[0]["period"], "2026-08")
        self.assertEqual(ledger.summarize(window=(0, 4000)), before)  # sealed intact
        self.assertIn("runtime/r1", ledger.summarize())  # resource not erased

    def test_correction_outside_sealed_applies_normally(self):
        # sealed watermark ends at t1000; the late trusted stop at t2000 is
        # outside it -> appended, projection recomputes (uncertain gap closes)
        ledger = self._lost_ledger()
        sealed = ({"period": "2026-08", "start_ms": 0, "end_ms": 1000},)
        late = envelope("manual", 101, {"type": "runtime.stopped",
                                        "resource_type": "runtime",
                                        "resource_id": "r1",
                                        "effective_at_ms": 2000,
                                        "recorded_at_ms": 5000})
        receipts = ledger.apply_correction([late], sealed)
        self.assertEqual(receipts[0]["status"], "appended")
        row = ledger.summarize()["runtime/r1"]
        self.assertEqual(row["confirmed_quantity_ms"], {"cpu_reserved": 2_000_000})
        self.assertEqual(row["uncertain_ms"], 0)

    def test_sealed_window_boundaries_are_half_open(self):
        ledger = self._lost_ledger()
        at_start = envelope("m0", 102, {"type": "runtime.stopped",
                                        "resource_type": "runtime",
                                        "resource_id": "r1",
                                        "effective_at_ms": 0,
                                        "recorded_at_ms": 5000})
        at_end = envelope("m1", 103, {"type": "runtime.stopped",
                                      "resource_type": "runtime",
                                      "resource_id": "r1",
                                      "effective_at_ms": 4000,
                                      "recorded_at_ms": 5000})
        receipts = ledger.apply_correction([at_start, at_end], self.SEALED)
        self.assertEqual(receipts[0]["status"], "flagged_manual")  # start_ms inside
        self.assertEqual(receipts[1]["status"], "appended")        # end_ms outside

    def test_flagged_correction_still_validates_envelope(self):
        ledger = self._lost_ledger()
        bogus = envelope("m", 104, {"type": "runtime.exploded",
                                  "resource_type": "runtime",
                                  "resource_id": "r1",
                                  "effective_at_ms": 2000})
        with self.assertRaises(ValueError):
            ledger.apply_correction([bogus], self.SEALED)

    def test_multiple_sealed_periods_all_enforced(self):
        ledger = self._lost_ledger()
        sealed = ({"period": "2026-07", "start_ms": 0, "end_ms": 500},
                  {"period": "2026-08", "start_ms": 3500, "end_ms": 9000})
        late = envelope("m", 105, {"type": "runtime.stopped",
                                   "resource_type": "runtime",
                                   "resource_id": "r1",
                                   "effective_at_ms": 3600,
                                   "recorded_at_ms": 9500})
        receipts = ledger.apply_correction([late], sealed)
        self.assertEqual((receipts[0]["status"], receipts[0]["period"]),
                         ("flagged_manual", "2026-08"))


class MutationGuards(unittest.TestCase):
    """Each new safeguard is load-bearing: break it, an assertion fails."""

    def test_tampered_fixture_total_fails_check(self):
        tampered = copy.deepcopy(by_name("two-snapshots-delete-one"))
        tampered["expected"]["snapshot/s1"]["confirmed_quantity_ms"][
            "snapshot_stored"] += 1
        ledger, _, receipts = run_fixture(tampered)
        with self.assertRaises(AssertionError):
            check_case(tampered, ledger, receipts)

    def test_sealed_period_check_guard(self):
        # without the sealed check the correction IS auto-applied and the
        # sealed window total is restated 1,000,000 -> 2,000,000
        case = by_name("lost-correction-in-sealed-month")
        with mock.patch.object(EventLedger, "_sealed_period_hit",
                               lambda self, event, periods: None):
            ledger, _, receipts = run_fixture(case)
            self.assertEqual(receipts[0]["status"], "appended")  # guard bypassed
            restated = ledger.summarize(window=(0, 4000))
        self.assertEqual(
            restated["runtime/r1"]["confirmed_quantity_ms"],
            {"cpu_reserved": 2_000_000})  # sealed history rewritten — bad
        real_ledger, _, _ = run_fixture(case)
        self.assertEqual(
            real_ledger.summarize(window=(0, 4000))["runtime/r1"]
            ["confirmed_quantity_ms"], {"cpu_reserved": 1_000_000})

    def test_purge_close_guard(self):
        # if volume.purged stops closing intervals, the trashed volume never
        # ends accrual and fixture totals change
        case = by_name("volume-trashed-while-sandbox-active")
        no_purge = resource_events._CLOSE - {"volume.purged"}
        with mock.patch.object(resource_events, "_CLOSE", no_purge):
            ledger, _, _ = run_fixture(case)
            broken = ledger.summarize()["workspace_volume/v1"]
        self.assertEqual(broken["confirmed_quantity_ms"], {})  # never closed
        self.assertIsNotNone(broken["open_capacity"])
        real_ledger, _, _ = run_fixture(case)
        self.assertEqual(
            real_ledger.summarize()["workspace_volume/v1"]["confirmed_quantity_ms"],
            {"volume_provisioned": 6_144_000})

    def test_snapshot_expiry_not_release_guard(self):
        # if snapshot.expired (the TTL delete REQUEST) were treated as a
        # release, accrual would stop at expiry instead of confirmed delete
        case = by_name("snapshot-ttl-expired-delete-unconfirmed")
        expired_closes = resource_events._CLOSE | {"snapshot.expired"}
        with mock.patch.object(resource_events, "_CLOSE", expired_closes):
            ledger, _, _ = run_fixture(case)
            broken = ledger.summarize()["snapshot/s1"]["confirmed_quantity_ms"]
        self.assertEqual(broken, {"snapshot_stored": 2048 * 2000})  # truncated — bad
        real_ledger, _, _ = run_fixture(case)
        self.assertEqual(
            real_ledger.summarize()["snapshot/s1"]["confirmed_quantity_ms"],
            {"snapshot_stored": 2048 * 6500})

    def test_trash_marker_stays_audit_only_guard(self):
        # if volume.trashed cut the interval, accrual would stop at trash
        case = by_name("volume-trashed-while-sandbox-active")
        trash_cuts = resource_events._SWITCH | {"volume.trashed"}
        with mock.patch.object(resource_events, "_SWITCH", trash_cuts):
            ledger, _, _ = run_fixture(case)
            broken = ledger.summarize()["workspace_volume/v1"]["confirmed_quantity_ms"]
        self.assertEqual(broken, {"volume_provisioned": 1024 * 3000})  # truncated
        real_ledger, _, _ = run_fixture(case)
        self.assertEqual(
            real_ledger.summarize()["workspace_volume/v1"]["confirmed_quantity_ms"],
            {"volume_provisioned": 1024 * 6000})


if __name__ == "__main__":
    unittest.main()
