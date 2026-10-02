"""Guard tests for the #77 minimal resource-event ledger slice.

Covers the issue scenarios without a Runner: full lifecycle with cold
suspend (compute stops at the confirmed stop, volumes keep accruing), destroy
and volume retention, restart persistence, replay vs inventory (no double
count), Lost gap marking, the correction path, outbox redelivery dedupe, the
no-currency rule, and mutation guards proving each safeguard is load-bearing
(pattern per scripts/test_persistence_contract.py).
"""
import copy
import itertools
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from resource_events import EventLedger  # noqa: E402

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "docs/contracts/ledger-examples.json").read_text())

_IDS = itertools.count(1)


def event(etype, rtype, rid, effective_ms, quantity=None, payload=None, **extra):
    payload = dict(payload or {})
    if quantity is not None:
        payload["quantity"] = quantity
    ev = {
        "schema_version": 1,
        "event_id": f"evt-{next(_IDS)}",
        "tenant_id": "t1",
        "workspace_id": "ws1",
        "resource_type": rtype,
        "resource_id": rid,
        "sandbox_id": "sb1",
        "source_id": "runner-1",
        "generation": 1,
        "source_seq": next(_IDS),
        "effective_at_ms": effective_ms,
        "recorded_at_ms": effective_ms,
        "type": etype,
        "reason": "test",
        "payload": payload,
        "certainty": "confirmed",
    }
    ev.update(extra)
    return ev


def lifecycle_ledger():
    """create -> Active -> Idle -> cold Suspend -> resume -> destroy.

    t0     provision workspace volume v1 (1024 B) and home volume h1 (512 B),
           runtime r1 starts Active (2000 milliCPU, 2 MiB); one audit-only op
    t1000  policy.applied Idle (500 milliCPU) — same resource, resize cut
    t2000  runtime.stopped r1: cold suspend, compute stops at the CONFIRMED stop
    t3000  resume: runtime r2 (new generation => new resource id), 1000 milliCPU
    t4000  destroy: r2 stopped, v1 and h1 released only here (retained through
           suspend; volumes meter until their own confirmed release)
    """
    ledger = EventLedger()
    for ev in [
        event("operation.requested", "sandbox", "sb1", 0, payload={"operation_id": "op-1"}),
        event("resource.provisioned", "workspace_volume", "v1", 0,
              quantity={"volume_provisioned": 1024}),
        event("resource.provisioned", "home_volume", "h1", 0,
              quantity={"volume_provisioned": 512}),
        event("runtime.started", "runtime", "r1", 0,
              quantity={"cpu_reserved": 2000, "memory_reserved": 2 << 20}),
        event("policy.applied", "runtime", "r1", 1000,
              quantity={"cpu_reserved": 500, "memory_reserved": 2 << 20}),
        event("runtime.stopped", "runtime", "r1", 2000),
        event("runtime.started", "runtime", "r2", 3000, generation=2,
              quantity={"cpu_reserved": 1000, "memory_reserved": 2 << 20}),
        event("runtime.stopped", "runtime", "r2", 4000),
        event("resource.released", "workspace_volume", "v1", 4000),
        event("resource.released", "home_volume", "h1", 4000),
    ]:
        receipt = ledger.append(ev)
        assert receipt["status"] == "appended", receipt
    return ledger


def lost_ledger():
    """Active from t0, last trusted heartbeat t1000, lease expired t3000."""
    ledger = EventLedger()
    ledger.append(event("runtime.started", "runtime", "r1", 0,
                        quantity={"cpu_reserved": 1000}))
    ledger.append(event("heartbeat.confirmed", "runtime", "r1", 1000))
    ledger.append(event("lease.expired", "runtime", "r1", 3000,
                        payload={"last_observed_at_ms": 1000}))
    return ledger


class Lifecycle(unittest.TestCase):
    def test_full_lifecycle_totals(self):
        totals = lifecycle_ledger().summarize()
        self.assertEqual(totals["runtime/r1"]["confirmed_quantity_ms"],
                         {"cpu_reserved": 2000 * 1000 + 500 * 1000,
                          "memory_reserved": (2 << 20) * 2000})
        self.assertEqual(totals["runtime/r2"]["confirmed_quantity_ms"],
                         {"cpu_reserved": 1000 * 1000, "memory_reserved": (2 << 20) * 1000})
        # volumes accrue from 0 to their own confirmed release at destroy,
        # straight through the suspend window (2000..3000)
        self.assertEqual(totals["workspace_volume/v1"]["confirmed_quantity_ms"],
                         {"volume_provisioned": 1024 * 4000})
        self.assertEqual(totals["home_volume/h1"]["confirmed_quantity_ms"],
                         {"volume_provisioned": 512 * 4000})
        for row in totals.values():
            self.assertEqual(row["uncertain_ms"], 0)
            self.assertIsNone(row["open_capacity"])

    def test_operation_events_are_audit_only(self):
        ledger = lifecycle_ledger()
        self.assertNotIn("sandbox/sb1", ledger.summarize())

    def test_destroy_without_release_keeps_volume_open(self):
        # persistence contract: destroy releases volumes only on confirmed
        # removal; a volume with no release event stays open (retained), and
        # an open interval is never integrated into confirmed totals
        ledger = EventLedger()
        ledger.append(event("resource.provisioned", "workspace_volume", "v1", 0,
                            quantity={"volume_provisioned": 1024}))
        row = ledger.summarize()["workspace_volume/v1"]
        self.assertEqual(row["confirmed_quantity_ms"], {})
        self.assertEqual(row["open_capacity"],
                         {"start_ms": 0, "quantity": {"volume_provisioned": 1024}})

    def test_snapshot_survives_destroy_and_ttl_expiry_is_not_release(self):
        ledger = lifecycle_ledger()
        ledger.append(event("snapshot.created", "snapshot", "s1", 0,
                            quantity={"snapshot_stored": 2048}))
        ledger.append(event("snapshot.expired", "snapshot", "s1", 2000))  # delete request only
        ledger.append(event("snapshot.deleted", "snapshot", "s1", 5000))
        row = ledger.summarize()["snapshot/s1"]
        # metering runs to the confirmed deletion at t5000, past sandbox
        # destroy (t4000) and past the TTL expiry request (t2000)
        self.assertEqual(row["confirmed_quantity_ms"], {"snapshot_stored": 2048 * 5000})

    def test_same_state_resize_cuts_both_intervals(self):
        ledger = EventLedger()
        ledger.append(event("runtime.started", "runtime", "r1", 0,
                            quantity={"cpu_reserved": 1000}))
        ledger.append(event("policy.applied", "runtime", "r1", 1000,
                            quantity={"cpu_reserved": 2000}))
        ledger.append(event("runtime.stopped", "runtime", "r1", 2000))
        self.assertEqual(ledger.summarize()["runtime/r1"]["confirmed_quantity_ms"],
                         {"cpu_reserved": 3_000_000})

    def test_zero_length_interval_is_zero(self):
        ledger = EventLedger()
        ledger.append(event("runtime.started", "runtime", "r1", 0,
                            quantity={"cpu_reserved": 1000}))
        ledger.append(event("policy.applied", "runtime", "r1", 0,
                            quantity={"cpu_reserved": 2000}))
        ledger.append(event("runtime.stopped", "runtime", "r1", 1000))
        row = ledger.summarize()["runtime/r1"]
        self.assertEqual(row["confirmed_quantity_ms"], {"cpu_reserved": 2_000_000})
        segments = ledger.projection("runtime/r1")["runtime/r1"]["segments"]
        self.assertEqual(len(segments), 1)

    def test_late_event_uses_effective_time_not_arrival(self):
        ledger = EventLedger()
        ledger.append(event("runtime.started", "runtime", "r1", 0,
                            quantity={"cpu_reserved": 1000}))
        late = event("runtime.stopped", "runtime", "r1", 2000)
        late["recorded_at_ms"] = 9000  # arrived 7s after it took effect
        ledger.append(late)
        self.assertEqual(ledger.summarize()["runtime/r1"]["confirmed_quantity_ms"],
                         {"cpu_reserved": 2_000_000})


class Validation(unittest.TestCase):
    def test_missing_effective_time_rejected(self):
        ev = event("runtime.started", "runtime", "r1", 0, quantity={"cpu_reserved": 1})
        del ev["effective_at_ms"]
        with self.assertRaises(ValueError):
            EventLedger().append(ev)

    def test_bad_certainty_rejected(self):
        ev = event("runtime.started", "runtime", "r1", 0, quantity={"cpu_reserved": 1})
        ev["certainty"] = "maybe"
        with self.assertRaises(ValueError):
            EventLedger().append(ev)

    def test_float_quantity_rejected(self):
        ev = event("runtime.started", "runtime", "r1", 0,
                   quantity={"cpu_reserved": 1.5})  # no JS Number, integers only
        with self.assertRaises(ValueError):
            EventLedger().append(ev)

    def test_unknown_type_rejected(self):
        ev = event("runtime.exploded", "runtime", "r1", 0)
        with self.assertRaises(ValueError):
            EventLedger().append(ev)

    def test_ledger_seq_monotonic_per_resource(self):
        ledger = EventLedger()
        r1a = ledger.append(event("runtime.started", "runtime", "r1", 0,
                                  quantity={"cpu_reserved": 1}))
        v1a = ledger.append(event("resource.provisioned", "workspace_volume", "v1", 0,
                                  quantity={"volume_provisioned": 1}))
        r1b = ledger.append(event("runtime.stopped", "runtime", "r1", 100))
        self.assertEqual([r1a["ledger_seq"], r1b["ledger_seq"]], [1, 2])
        self.assertEqual(v1a["ledger_seq"], 1)  # independent counter per resource
        ok = event("runtime.started", "runtime", "r2", 200, quantity={"cpu_reserved": 1})
        ok["ledger_seq"] = 1
        self.assertEqual(ledger.append(ok)["ledger_seq"], 1)
        bad = event("resource.released", "workspace_volume", "v1", 300)
        bad["ledger_seq"] = 7  # next for v1 is 2
        with self.assertRaises(ValueError):
            ledger.append(bad)


class DedupeAndConflicts(unittest.TestCase):
    def test_exact_duplicate_returns_existing_receipt(self):
        ledger = lifecycle_ledger()
        before = ledger.summarize()
        first = ledger.events("runtime/r1")[0]
        redelivered = dict(first)  # identical content, new object
        receipt = ledger.append(redelivered)
        self.assertEqual(receipt["status"], "duplicate")
        self.assertEqual(receipt["ledger_seq"], first["ledger_seq"])
        self.assertEqual(len(ledger.events()), 10)
        self.assertEqual(ledger.summarize(), before)  # no double count

    def test_same_event_id_different_content_quarantined(self):
        ledger = lifecycle_ledger()
        before = ledger.summarize()
        original = ledger.events("runtime/r1")[0]
        clash = dict(original, reason="tampered")
        receipt = ledger.append(clash)
        self.assertEqual(receipt["status"], "quarantined")
        self.assertTrue(ledger.flags())
        self.assertEqual(len(ledger.quarantine()), 1)
        # the original is intact and still drives the projection
        stored = next(e for e in ledger.events() if e["event_id"] == original["event_id"])
        self.assertEqual(stored["reason"], original["reason"])
        self.assertEqual(ledger.summarize(), before)

    def test_same_source_key_different_content_quarantined(self):
        ledger = EventLedger()
        first = event("runtime.started", "runtime", "r1", 0,
                      quantity={"cpu_reserved": 1000})
        ledger.append(first)
        clash = dict(first, event_id="evt-other", reason="different content")
        receipt = ledger.append(clash)
        self.assertEqual(receipt["status"], "quarantined")
        self.assertEqual(len(ledger.events()), 1)

    def test_ledger_assigned_fields_do_not_break_redelivery(self):
        ledger = EventLedger()
        first_receipt = ledger.append(event("runtime.started", "runtime", "r1", 0,
                                            quantity={"cpu_reserved": 1000}))
        stored = ledger.events("runtime/r1")[0]
        redelivered = dict(stored)
        for field in ("ledger_seq", "recorded_at_ms"):  # control plane assigns these
            del redelivered[field]
        receipt = ledger.append(redelivered)
        self.assertEqual(receipt["status"], "duplicate")
        self.assertEqual(receipt["ledger_seq"], first_receipt["ledger_seq"])


class Uncertainty(unittest.TestCase):
    def test_lost_marks_gap_never_active_never_zero(self):
        ledger = lost_ledger()
        row = ledger.summarize()["runtime/r1"]
        # confirmed only up to the last trusted heartbeat H=1000
        self.assertEqual(row["confirmed_quantity_ms"], {"cpu_reserved": 1_000_000})
        self.assertNotEqual(row["confirmed_quantity_ms"], {"cpu_reserved": 3_000_000})  # not Active
        self.assertNotEqual(row["confirmed_quantity_ms"], {})  # not zero
        self.assertEqual(row["uncertain_ms"], 2000)
        self.assertEqual(row["uncertain_capacity_ranges"],
                         [{"start_ms": 1000, "end_ms": 3000,
                           "quantity": {"cpu_reserved": 1000}}])
        self.assertIsNone(row["open_capacity"])

    def test_correction_recomputes_and_keeps_both_versions(self):
        ledger = lost_ledger()
        before = ledger.summarize()["runtime/r1"]
        # recovery produces a trusted stop at t2000, appended after the fact
        late = event("runtime.stopped", "runtime", "r1", 2000)
        late["recorded_at_ms"] = 3500
        ledger.append(late)
        after = ledger.summarize()["runtime/r1"]
        self.assertEqual(after["confirmed_quantity_ms"], {"cpu_reserved": 2_000_000})
        self.assertEqual(after["uncertain_ms"], 0)
        # both versions kept: the superseded uncertain gap stays in the archive
        superseded = ledger.superseded("runtime/r1")
        uncertain_old = [seg for seg in superseded if seg["certainty"] == "uncertain"]
        self.assertEqual(len(uncertain_old), 1)
        self.assertEqual((uncertain_old[0]["start_ms"], uncertain_old[0]["end_ms"]),
                         (1000, 3000))
        self.assertGreater(before["uncertain_ms"], after["uncertain_ms"])

    def test_correction_stop_exactly_at_recovery_instant(self):
        ledger = lost_ledger()
        # trusted stop effective exactly at R (the lease.expired instant 3000)
        late = event("runtime.stopped", "runtime", "r1", 3000)
        late["recorded_at_ms"] = 3500
        ledger.append(late)
        after = ledger.summarize()["runtime/r1"]
        self.assertEqual(after["confirmed_quantity_ms"], {"cpu_reserved": 3_000_000})
        self.assertEqual(after["uncertain_ms"], 0)
        uncertain_old = [seg for seg in ledger.superseded("runtime/r1")
                         if seg["certainty"] == "uncertain"]
        self.assertEqual(len(uncertain_old), 1)


class PersistenceAndReplay(unittest.TestCase):
    def test_restart_keeps_events_and_totals(self):
        ledger = lifecycle_ledger()
        revived = EventLedger.from_json(ledger.to_json())
        self.assertEqual(revived.events(), ledger.events())
        self.assertEqual(revived.summarize(), ledger.summarize())
        self.assertEqual(revived.superseded(), ledger.superseded())
        # sequence continues, not reset
        nxt = revived.append(event("resource.resized", "workspace_volume", "v1", 5000,
                                   quantity={"volume_provisioned": 2048}))
        self.assertEqual(nxt["ledger_seq"], 3)

    def test_replay_into_fresh_ledger_matches_inventory(self):
        ledger = lost_ledger()
        ledger.append(event("runtime.stopped", "runtime", "r1", 2000))
        replayed = EventLedger()
        for ev in ledger.events():
            self.assertEqual(replayed.append(ev)["status"], "appended")
        self.assertEqual(replayed.summarize(), ledger.summarize())

    def test_full_redelivery_does_not_double_count(self):
        ledger = lifecycle_ledger()
        before = ledger.summarize()
        for ev in ledger.events():  # the whole batch arrives twice
            receipt = ledger.append(ev)
            self.assertEqual(receipt["status"], "duplicate")
        self.assertEqual(len(ledger.events()), 10)
        self.assertEqual(ledger.summarize(), before)


class Outbox(unittest.TestCase):
    def test_state_and_outbox_commit_under_one_id(self):
        ledger = EventLedger()
        state = event("policy.applied", "runtime", "r1", 1000,
                      quantity={"cpu_reserved": 500})
        receipt = ledger.commit_with_outbox(state, {"topic": "state.confirmed"})
        self.assertEqual(receipt["commit_seq"], receipt["outbox_id"])
        self.assertEqual(receipt["event"]["status"], "appended")
        pending = ledger.pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event_id"], state["event_id"])
        ledger.mark_outbox_delivered(receipt["outbox_id"])
        self.assertEqual(ledger.pending_outbox(), [])

    def test_redelivery_returns_same_id_without_second_entry(self):
        ledger = EventLedger()
        state = event("runtime.stopped", "runtime", "r1", 2000)
        first = ledger.commit_with_outbox(state, {"topic": "state.confirmed"})
        again = ledger.commit_with_outbox(dict(state), {"topic": "state.confirmed"})
        self.assertEqual(again["commit_seq"], first["commit_seq"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(ledger.pending_outbox()), 1)

    def test_unknown_outbox_id_rejected(self):
        with self.assertRaises(ValueError):
            EventLedger().mark_outbox_delivered(999)


class NoCurrency(unittest.TestCase):
    FORBIDDEN = ("rate", "cost", "price", "usd", "cent", "currency", "dollar",
                 "bill", "minor", "ntd", "twd", "charge", "amount_due")

    def _walk(self, node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key, value
                yield from self._walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from self._walk(item)

    def test_summarize_has_no_currency_or_rates(self):
        totals = {**lifecycle_ledger().summarize(), **lost_ledger().summarize()}
        for key, value in self._walk(totals):
            lowered = str(key).lower()
            for word in self.FORBIDDEN:
                self.assertNotIn(word, lowered, f"forbidden key {key!r}")
                if isinstance(value, str):
                    self.assertNotIn(word, value.lower())

    def test_summarize_uses_integers_only(self):
        for ledger in (lifecycle_ledger(), lost_ledger()):
            for _, value in self._walk(ledger.summarize()):
                if isinstance(value, float):
                    self.fail("float in summarize output")


class ContractExamples(unittest.TestCase):
    """Replay docs/contracts/ledger-examples.json scenarios through the ledger."""

    METER = {"cpu": "cpu_reserved", "volume": "volume_provisioned",
             "snapshot": "snapshot_stored"}
    RESOURCE = {"cpu": "runtime", "volume": "workspace_volume", "snapshot": "snapshot"}

    def _resource_type_of(self, bucket):
        if bucket in self.RESOURCE:
            return self.RESOURCE[bucket]
        return self.RESOURCE[bucket.split("/", 1)[1]]  # "YYYY-MM/cpu" -> cpu

    def _meter_of(self, bucket):
        if bucket in self.METER:
            return self.METER[bucket]
        return self.METER[bucket.split("/", 1)[1]]  # "YYYY-MM/cpu" -> cpu

    def _events_for(self, case):
        events, seq = [], itertools.count(1)
        rtype_cache = {}

        def make(etype, bucket, resource, effective, quantity=None, payload=None):
            rtype = rtype_cache.setdefault(bucket, self._resource_type_of(bucket))
            return event(etype, rtype, resource, effective, quantity=quantity,
                         payload=payload, source_seq=next(seq))

        by_resource = {}
        for seg in case["segments"]:
            by_resource.setdefault((seg["resource"], seg["bucket"]), []).append(seg)
        for (resource, bucket), segs in by_resource.items():
            segs.sort(key=lambda s: s["start_ms"])
            meter = self._meter_of(bucket)
            last_confirmed_end = None
            for seg in segs:
                if seg.get("certainty") == "uncertain":
                    # Lost: last trusted heartbeat at start, declared Lost at end
                    events.append(make("heartbeat.confirmed", bucket, resource,
                                       seg["start_ms"]))
                    events.append(make("lease.expired", bucket, resource, seg["end_ms"],
                                       payload={"last_observed_at_ms": seg["start_ms"]}))
                    continue
                kind = ("runtime.started" if meter == "cpu_reserved" else
                        "snapshot.created" if meter == "snapshot_stored" else
                        "resource.provisioned")
                events.append(make(kind if last_confirmed_end is None else
                                   "policy.applied" if meter == "cpu_reserved" else
                                   "resource.resized",
                                   bucket, resource, seg["start_ms"],
                                   quantity={meter: seg["quantity"]}))
                last_confirmed_end = seg["end_ms"]
            if segs and all(s.get("certainty") != "uncertain" for s in segs):
                close = ("runtime.stopped" if meter == "cpu_reserved" else
                         "snapshot.deleted" if meter == "snapshot_stored" else
                         "resource.released")
                events.append(make(close, bucket, resource, last_confirmed_end))
        return events

    def test_contract_examples_replay_to_expected_totals(self):
        for case in CONTRACT:
            with self.subTest(case=case["name"]):
                ledger = EventLedger()
                for ev in self._events_for(case):
                    self.assertEqual(ledger.append(ev)["status"], "appended")
                totals = ledger.summarize()
                confirmed = sum(sum(row["confirmed_quantity_ms"].values())
                                for row in totals.values())
                self.assertEqual(confirmed, sum(case["expected_units"].values()))
                uncertain = sum(row["uncertain_ms"] for row in totals.values())
                self.assertEqual(uncertain, case.get("expected_uncertain_resource_ms", 0))
                # rate-cased fixtures price nothing here (expected_minor is #25)


class MutationGuards(unittest.TestCase):
    """Each safeguard must be load-bearing: break it, the totals change."""

    def test_dedupe_guard_prevents_double_count(self):
        ledger = lifecycle_ledger()
        first = ledger.events("runtime/r1")[0]
        bypass = {k: v for k, v in first.items() if k != "ledger_seq"}
        with mock.patch.object(EventLedger, "_dedupe_check",
                               lambda self, event: ("new", None)):
            receipt = ledger.append(bypass)
            self.assertEqual(receipt["status"], "appended")  # guard bypassed
        # the redelivery is stored twice: replay/persistence would consume 11
        self.assertEqual(len(ledger.events()), 11)
        self.assertEqual(len(lifecycle_ledger().events()), 10)

    def test_conflict_isolation_guard_protects_history(self):
        ledger = lifecycle_ledger()
        original = ledger.events("runtime/r1")[0]
        clash = {k: v for k, v in original.items() if k != "ledger_seq"}
        clash["payload"] = {"quantity": {"cpu_reserved": 99999}}
        with mock.patch.object(EventLedger, "_dedupe_check",
                               lambda self, event: ("new", None)):
            ledger.append(clash)  # without quarantine the clash enters history
        self.assertNotEqual(ledger.summarize(), lifecycle_ledger().summarize())

    def test_effective_time_guard_blocks_arrival_fabrication(self):
        ledger = lost_ledger()
        late = event("runtime.stopped", "runtime", "r1", 2000)
        late["recorded_at_ms"] = 3500
        with mock.patch.object(EventLedger, "_projection_time",
                               lambda self, ev: ev["recorded_at_ms"]):
            fabricated = ledger.summarize()
        ledger.append(late)
        self.assertNotEqual(fabricated["runtime/r1"]["confirmed_quantity_ms"],
                            ledger.summarize()["runtime/r1"]["confirmed_quantity_ms"])

    def test_half_open_interval_guard(self):
        ledger = EventLedger()
        ledger.append(event("runtime.started", "runtime", "r1", 0,
                            quantity={"cpu_reserved": 1000}))
        ledger.append(event("runtime.stopped", "runtime", "r1", 1000))
        before = ledger.summarize()["runtime/r1"]["confirmed_quantity_ms"]
        original = EventLedger._fold

        def closed_intervals(self, events):
            folded = original(self, events)
            for seg in folded["segments"]:  # simulate closed [start, end]
                seg["end_ms"] += 1
            return folded

        with mock.patch.object(EventLedger, "_fold", closed_intervals):
            after = ledger.summarize()["runtime/r1"]["confirmed_quantity_ms"]
        self.assertEqual(before, {"cpu_reserved": 1_000_000})
        self.assertEqual(after, {"cpu_reserved": 1_001_000})  # inflated without guard

    def test_uncertain_exclusion_guard(self):
        ledger = lost_ledger()
        original = EventLedger._fold

        def count_uncertain_as_confirmed(self, events):
            folded = original(self, events)
            for seg in folded["segments"]:
                seg["certainty"] = "confirmed"
            return folded

        with mock.patch.object(EventLedger, "_fold", count_uncertain_as_confirmed):
            inflated = ledger.summarize()["runtime/r1"]
        self.assertEqual(inflated["confirmed_quantity_ms"], {"cpu_reserved": 3_000_000})
        self.assertEqual(ledger.summarize()["runtime/r1"]["confirmed_quantity_ms"],
                         {"cpu_reserved": 1_000_000})

    def test_outbox_redelivery_guard(self):
        ledger = EventLedger()
        state = event("runtime.stopped", "runtime", "r1", 2000)
        ledger.commit_with_outbox(state, {"topic": "state.confirmed"})
        with mock.patch.object(EventLedger, "_outbox_for", lambda self, event_id: None):
            ledger.commit_with_outbox(dict(state), {"topic": "state.confirmed"})
        self.assertEqual(len(ledger.pending_outbox()), 2)  # duplicated without guard


if __name__ == "__main__":
    unittest.main()
