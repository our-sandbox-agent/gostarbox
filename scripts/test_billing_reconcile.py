"""Fixtures and guard tests for the #28 billing reconciliation slice.

Coverage: column separation (a credit NEVER reduces units — issue
acceptance 不把 credit 當 units 被修好了), units-vs-money diff kinds,
pending-aggregate blocking (stripe_bridge ReconciliationBlocked reused),
async-delay tolerance (in-flight residual + ready deadline), lost-ack
recovery by idempotency id (no double adjustment), partial success
(failed meter retriable, confirmed meters never re-sent), cross-month
settlement against the usage engine (original month adjustment line,
sealed invoice immutable), draft-vs-finalized handling, correction
idempotence (apply twice -> once; full --repair rerun -> no duplicate
credits/refunds; snapshot restore -> evidence preserved), first-version
human approval (unapproved runs propose only), and mutation guards
proving each safeguard load-bearing (CONTRIBUTING mutate-and-fail
idiom, pattern per scripts/test_stripe_bridge.py).
"""
import calendar
import itertools
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import billing_reconcile  # noqa: E402
import stripe_bridge  # noqa: E402
import usage_engine  # noqa: E402
from billing_reconcile import (BillingReconciler, Correction,  # noqa: E402
                               CorrectionProviderDouble, CorrectionsLedger,
                               ReconcileRow, diff, local_lines,
                               to_adjustment_line)
from stripe_bridge import ReconciliationBlocked  # noqa: E402

SEP_1 = calendar.timegm((2026, 9, 1, 0, 0, 0)) * 1000
OCT_1 = calendar.timegm((2026, 10, 1, 0, 0, 0)) * 1000
NOW = calendar.timegm((2026, 10, 2, 0, 0, 0)) * 1000
DEADLINE = NOW + 3600_000

KEY = ("2026-10", "ws1", "cpu_reserved")


def local_map(key=KEY, units=100, minor=300, currency="USD"):
    return {key: {"units": units, "minor": minor, "currency": currency}}


def provider_map(key=KEY, status="ready", units=100, invoice_minor=300,
                 invoice_state="finalized", currency="USD"):
    return {key: {"status": status, "units": units, "currency": currency,
                  "invoice_minor": invoice_minor, "invoice_state": invoice_state}}


def rig():
    prov = CorrectionProviderDouble()
    ledger = CorrectionsLedger(prov.status)
    rec = BillingReconciler(ledger, prov.actions_map())
    return prov, ledger, rec


def make_correction(**over):
    base = dict(correction_id="corr:2026-10:ws1:cpu_reserved:money-next",
                kind="credit_note", month="2026-10", workspace_id="ws1",
                meter="cpu_reserved", currency="USD", minor=60,
                target="next_invoice", approved_by="ops@example",
                approved_at_ms=NOW)
    base.update(over)
    return Correction(**base)


def rate_card():
    card = usage_engine.RateCard()
    card.upsert({"rate_id": "cpu", "version": 1, "currency": "USD",
                 "meter": "cpu_reserved", "numerator_minor": 3,
                 "denominator_meter_units": 1000}, now_ms=0)
    return card


class ColumnSeparation(unittest.TestCase):
    def test_row_carries_all_five_columns(self):
        row = ReconcileRow(month="2026-10", workspace_id="ws1",
                           meter="cpu_reserved", currency="USD",
                           local_units=100, provider_units=80,
                           provider_status="ready", in_flight_units=20,
                           invoice_minor=300, invoice_state="finalized",
                           credits_applied_minor=60)
        columns = row.to_dict()
        for column in ("local_units", "provider_units", "in_flight_units",
                       "invoice_minor", "credits_applied_minor"):
            self.assertIn(column, columns)
        self.assertEqual(columns["credits_applied_minor"], 60)

    def test_credits_never_change_units_gap(self):
        d0 = diff({"units": 100, "minor": 300},
                  {"status": "ready", "units": 80, "invoice_minor": 300})
        credited = diff({"units": 100, "minor": 300},
                        {"status": "ready", "units": 80, "invoice_minor": 360,
                         "credits_minor": 60})
        self.assertEqual(d0["units_gap"], 20)
        self.assertEqual(credited["units_gap"], 20)  # credit fixed money only
        self.assertEqual(credited["money_gap"], 0)
        self.assertEqual(credited["kind"], "units_mismatch")

    def test_credit_covering_money_does_not_mask_units_mismatch(self):
        # invoice over-charged 60 AND 10 units missing; the 60 credit
        # zeroed the money side — the 10-unit gap is still reported
        d = diff({"units": 100, "minor": 300},
                 {"status": "ready", "units": 90, "invoice_minor": 360,
                  "credits_minor": 60})
        self.assertEqual(d["kind"], "units_mismatch")
        self.assertEqual(d["units_gap"], 10)
        self.assertEqual(d["money_gap"], 0)

    def test_correction_refuses_mixed_units_and_money(self):
        with self.assertRaises(ValueError):  # credit booked as units
            make_correction(kind="meter_adjustment", units=5, minor=-15,
                            target="meter")
        with self.assertRaises(ValueError):  # credit carrying units
            make_correction(units=5, minor=60)
        with self.assertRaises(ValueError):  # zero units "fix"
            make_correction(kind="meter_adjustment", units=0, minor=0,
                            target="meter")


class DiffJudgment(unittest.TestCase):
    def test_units_vs_money_kinds_distinguished(self):
        units = diff({"units": 100, "minor": 300},
                     {"status": "ready", "units": 90, "invoice_minor": 300})
        self.assertEqual(units["kind"], "units_mismatch")
        self.assertEqual(units["money_gap"], 0)
        money = diff({"units": 100, "minor": 300},
                     {"status": "ready", "units": 100, "invoice_minor": 360})
        self.assertEqual(money["kind"], "money_mismatch")
        self.assertEqual(money["units_gap"], 0)
        self.assertEqual(money["money_gap"], -60)

    def test_match_and_no_invoice(self):
        match = diff({"units": 100, "minor": 300},
                     {"status": "ready", "units": 100, "invoice_minor": 300})
        self.assertEqual(match["kind"], "match")
        none = diff({"units": 100, "minor": 300},
                    {"status": "ready", "units": 100, "invoice_minor": None})
        self.assertEqual(none["kind"], "no_invoice")
        self.assertIsNone(none["money_gap"])

    def test_input_validation(self):
        cases = [
            (([], {}), {}),
            (({"units": 1.5, "minor": 0},
              {"status": "ready", "units": 1}), {}),
            (({"units": 1, "minor": 0},
              {"status": "weird", "units": 1}), {}),
            (({"units": 1, "minor": 0},
              {"status": "ready", "units": None}), {}),
            (({"units": 1, "minor": 0},
              {"status": "ready", "units": 1, "credits_minor": "60"}), {}),
        ]
        for (local, provider), _ in cases:
            with self.subTest(local=local, provider=provider):
                with self.assertRaises(ValueError):
                    diff(local, provider)


class PendingAggregate(unittest.TestCase):
    def test_diff_pending_raises_stripe_bridge_blocked(self):
        # pending aggregate exposing a PROVISIONAL number: never judged
        with self.assertRaises(stripe_bridge.ReconciliationBlocked):
            diff({"units": 100, "minor": 300},
                 {"status": "pending", "units": 80, "invoice_minor": 300})

    def test_reconcile_awaiting_within_deadline(self):
        _, ledger, rec = rig()
        run = rec.reconcile(local_map(units=100),
                            provider_map(status="pending", units=80),
                            now_ms=NOW, ready_deadline_ms=DEADLINE)
        entry = run["rows"][0]
        self.assertEqual(entry["kind"], "awaiting_aggregation")
        self.assertNotIn("correction_id", entry)   # no judgment, no fix
        self.assertEqual(ledger.ids(), [])
        self.assertEqual(entry["row"]["in_flight_units"], 0)

    def test_reconcile_stalled_after_deadline(self):
        # escalation only: STILL no units/money judgment against pending
        _, ledger, rec = rig()
        run = rec.reconcile(local_map(units=100),
                            provider_map(status="pending", units=80),
                            now_ms=NOW, ready_deadline_ms=NOW - 1)
        entry = run["rows"][0]
        self.assertEqual(entry["kind"], "stalled")
        self.assertIsNone(entry["units_gap"])
        self.assertEqual(ledger.ids(), [])


class AsyncDelay(unittest.TestCase):
    def test_in_flight_residual_tolerated(self):
        # local 100; provider aggregated 80; 20 sent-but-not-aggregated
        _, _, rec = rig()
        run = rec.reconcile(local_map(units=100, minor=300),
                            provider_map(units=80, invoice_minor=300),
                            in_flight={KEY: 20}, now_ms=NOW)
        self.assertEqual(run["rows"][0]["kind"], "match")
        self.assertEqual(run["rows"][0]["row"]["in_flight_units"], 20)

    def test_in_flight_partial_residual_is_mismatch(self):
        _, _, rec = rig()
        run = rec.reconcile(local_map(units=100, minor=300),
                            provider_map(units=80, invoice_minor=300),
                            in_flight={KEY: 15}, now_ms=NOW)
        self.assertEqual(run["rows"][0]["kind"], "units_mismatch")
        self.assertEqual(run["rows"][0]["units_gap"], 5)


class LostAckRecovery(unittest.TestCase):
    def test_lost_ack_recovered_by_idempotency_query_no_double_adjustment(self):
        prov, ledger, rec = rig()
        calls = []

        def flaky(correction):
            calls.append(correction.correction_id)
            prov.credit_note(correction)      # provider ACCEPTS...
            raise TimeoutError("local ack lost after API accept")

        rec._actions["credit_note"] = flaky
        inputs = dict(now_ms=NOW, approved_by="ops@example")
        run1 = rec.reconcile(local_map(minor=300),
                             provider_map(invoice_minor=360), **inputs)
        self.assertEqual(run1["rows"][0]["apply_status"], "failed")
        self.assertEqual(prov.effects, 1)      # accepted remotely
        self.assertEqual(ledger.ids(), [])     # nothing recorded locally

        run2 = rec.reconcile(local_map(minor=300),
                             provider_map(invoice_minor=360), **inputs)
        entry = run2["rows"][0]
        self.assertEqual(entry["apply_status"], "recovered")
        self.assertEqual(len(calls), 1)        # action never re-run (READ)
        self.assertEqual(prov.effects, 1)      # no double adjustment
        self.assertEqual(ledger.entry(entry["correction_id"])["status"],
                         "recovered")


class PartialSuccess(unittest.TestCase):
    def test_failed_meter_retriable_confirmed_not_resent(self):
        prov, ledger, rec = rig()
        ka = ("2026-10", "ws1", "cpu_reserved")
        kb = ("2026-10", "ws1", "memory_reserved")
        kc = ("2026-10", "ws2", "cpu_reserved")
        local = {ka: {"units": 100, "minor": 300, "currency": "USD"},
                 kb: {"units": 100, "minor": 200, "currency": "USD"},
                 kc: {"units": 100, "minor": 300, "currency": "USD"}}
        provider = {k: {"status": "ready", "units": 90, "currency": "USD",
                        "invoice_minor": 270, "invoice_state": "draft"}
                    for k in (ka, kb, kc)}
        counts = {}

        def meter_action(correction):
            counts[correction.correction_id] = \
                counts.get(correction.correction_id, 0) + 1
            if (correction.workspace_id == "ws2"
                    and counts[correction.correction_id] == 1):
                raise RuntimeError("provider 500")
            return prov.meter_adjustment(correction)

        rec._actions["meter_adjustment"] = meter_action

        def by_key(run):
            return {(r["row"]["workspace_id"], r["row"]["meter"]): r
                    for r in run["rows"]}

        run1 = by_key(rec.reconcile(local, provider, now_ms=NOW,
                                    approved_by="ops"))
        self.assertEqual(run1[("ws1", "cpu_reserved")]["apply_status"],
                         "applied")
        self.assertEqual(run1[("ws1", "memory_reserved")]["apply_status"],
                         "applied")
        self.assertEqual(run1[("ws2", "cpu_reserved")]["apply_status"],
                         "failed")
        self.assertIn("provider 500", run1[("ws2", "cpu_reserved")]["error"])

        run2 = by_key(rec.reconcile(local, provider, now_ms=NOW,
                                    approved_by="ops"))
        self.assertEqual(run2[("ws1", "cpu_reserved")]["apply_status"],
                         "already_applied")   # confirmed: never re-sent
        self.assertEqual(run2[("ws1", "memory_reserved")]["apply_status"],
                         "already_applied")
        self.assertEqual(run2[("ws2", "cpu_reserved")]["apply_status"],
                         "applied")           # failed meter retried OK
        self.assertEqual(sorted(counts.values()), [1, 1, 2])
        self.assertEqual(prov.effects, 3)


class CrossMonth(unittest.TestCase):
    def _scenario(self):
        card = rate_card()
        segment = {"start_ms": SEP_1, "end_ms": SEP_1 + 2000,
                   "workspace_id": "ws1", "quantity": {"cpu_reserved": 500}}
        frozen = {"month": "2026-09", "workspace_id": "ws1", "currency": "USD",
                  "meter": "cpu_reserved", "units": 1_000_000, "minor": 3000,
                  "rate_versions": ["cpu v1"]}
        sealed = {"period": "2026-09", "start_ms": SEP_1, "end_ms": OCT_1,
                  "lines": [frozen]}
        view = usage_engine.usage_view(card, [segment],
                                       sealed_periods=[sealed])
        local = local_lines(view)
        key = ("2026-09", "ws1", "cpu_reserved")
        self.assertEqual(local[key]["units"], 1_000_000)  # from usage engine
        provider = {key: {"status": "ready", "units": 1_000_000,
                          "currency": "USD", "invoice_minor": 3060,
                          "invoice_state": "finalized"}}  # over-charged 60
        return card, segment, sealed, view, local, provider, key

    def test_correction_belongs_to_original_month_original_immutable(self):
        card, segment, sealed, view, local, provider, key = self._scenario()
        _, ledger, rec = rig()
        run = rec.reconcile(local, provider, now_ms=NOW,
                            approved_by="ops@example")
        self.assertEqual(run["rows"][0]["kind"], "money_mismatch")
        entry = ledger.entry(run["rows"][0]["correction_id"])
        correction = entry["correction"]
        self.assertEqual(correction["kind"], "credit_note")
        self.assertEqual(correction["month"], "2026-09")   # ORIGINAL month
        self.assertEqual(correction["target"], "next_invoice")

        line = to_adjustment_line(Correction(**correction))
        self.assertEqual(line["period"], "2026-09")        # 歸原月調整線
        self.assertEqual(line["minor"], -60)
        self.assertEqual(line["units"], 0)                 # money, not units

        after = usage_engine.usage_view(card, [segment], sealed_periods=[sealed],
                                        adjustments=[line])
        sealed_before = [l for l in view["lines"] if l["kind"] == "sealed"]
        sealed_after = [l for l in after["lines"] if l["kind"] == "sealed"]
        self.assertEqual(sealed_after, sealed_before)      # original immutable
        self.assertEqual(after["totals"], view["totals"])  # never recomputed
        adjustment = [l for l in after["lines"] if l["kind"] == "adjustment"]
        self.assertEqual(adjustment[0]["month"], "2026-09")
        self.assertEqual(adjustment[0]["minor"], -60)
        self.assertEqual(after["adjustment_totals"], {"USD": -60})

    def test_unit_fixes_never_become_invoice_lines(self):
        with self.assertRaises(ValueError):
            to_adjustment_line(make_correction(
                kind="meter_adjustment", units=-10, minor=0, target="meter"))


class DraftFinalized(unittest.TestCase):
    def test_draft_invoice_amendable_in_place(self):
        _, ledger, rec = rig()
        run = rec.reconcile(local_map(minor=300),
                            provider_map(invoice_minor=360,
                                         invoice_state="draft"),
                            now_ms=NOW, approved_by="ops")
        entry = run["rows"][0]
        self.assertEqual(entry["apply_status"], "applied")
        self.assertTrue(entry["correction_id"].endswith("money-draft"))
        correction = ledger.entry(entry["correction_id"])["correction"]
        self.assertEqual(correction["kind"], "draft_amendment")
        self.assertEqual(correction["target"], "draft_line")

    def test_finalized_invoice_next_invoice_original_untouched(self):
        _, ledger, rec = rig()
        run = rec.reconcile(local_map(minor=300),
                            provider_map(invoice_minor=360,
                                         invoice_state="finalized"),
                            now_ms=NOW, approved_by="ops")
        entry = run["rows"][0]
        correction = ledger.entry(entry["correction_id"])["correction"]
        self.assertEqual(correction["kind"], "credit_note")
        self.assertEqual(correction["target"], "next_invoice")
        self.assertTrue(entry["correction_id"].endswith("money-next"))

    def test_draft_and_finalized_use_distinct_provider_actions(self):
        prov, ledger, rec = rig()
        rec.reconcile(local_map(minor=300),
                      provider_map(invoice_minor=360, invoice_state="draft"),
                      now_ms=NOW, approved_by="ops")
        self.assertEqual(prov.calls.get("draft_amendment"), 1)
        self.assertEqual(prov.calls.get("credit_note", 0), 0)
        prov2, ledger2, rec2 = rig()
        rec2.reconcile(local_map(minor=300),
                       provider_map(invoice_minor=360,
                                    invoice_state="finalized"),
                       now_ms=NOW, approved_by="ops")
        self.assertEqual(prov2.calls.get("credit_note"), 1)
        self.assertEqual(prov2.calls.get("draft_amendment", 0), 0)


class Idempotence(unittest.TestCase):
    def test_apply_twice_is_noop(self):
        prov, ledger, _ = rig()
        correction = make_correction()
        first = ledger.apply(correction, prov.credit_note, now_ms=NOW)
        second = ledger.apply(correction, prov.credit_note, now_ms=NOW)
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "already_applied")
        self.assertFalse(second["changed"])
        self.assertEqual(prov.effects, 1)
        self.assertEqual(prov.calls["credit_note"], 1)

    def test_units_rerun_already_applied(self):
        prov, ledger, rec = rig()
        inputs = dict(now_ms=NOW, approved_by="ops")
        local, provider = local_map(minor=300), provider_map(units=90)
        run1 = rec.reconcile(local, provider, **inputs)
        self.assertEqual(run1["rows"][0]["apply_status"], "applied")
        run2 = rec.reconcile(local, provider, **inputs)   # --repair rerun
        self.assertEqual(run2["rows"][0]["apply_status"], "already_applied")
        self.assertEqual(prov.effects, 1)
        self.assertEqual(prov.calls["meter_adjustment"], 1)  # no re-send

    def test_money_rerun_nets_to_match(self):
        prov, ledger, rec = rig()
        inputs = dict(now_ms=NOW, approved_by="ops")
        local = local_map(minor=300)
        provider = provider_map(invoice_minor=360)
        run1 = rec.reconcile(local, provider, **inputs)
        self.assertEqual(run1["rows"][0]["apply_status"], "applied")
        run2 = rec.reconcile(local, provider, **inputs)   # --repair rerun
        self.assertEqual(run2["rows"][0]["kind"], "match")  # credit netted
        self.assertEqual(run2["summary"], {"match": 1})
        self.assertEqual(prov.effects, 1)   # no duplicate refund/credit

    def test_snapshot_restore_preserves_evidence_and_no_dup(self):
        prov, ledger, rec = rig()
        local, provider = local_map(minor=300), provider_map(units=90)
        rec.reconcile(local, provider, now_ms=NOW, approved_by="ops")
        snapshot = ledger.to_dict()          # daily backup image
        restored = CorrectionsLedger.from_dict(snapshot, prov.status)
        rec2 = BillingReconciler(restored, prov.actions_map())
        run = rec2.reconcile(local, provider, now_ms=NOW, approved_by="ops")
        self.assertEqual(run["rows"][0]["apply_status"], "already_applied")
        self.assertEqual(prov.effects, 1)
        entry = restored.entry(run["rows"][0]["correction_id"])
        self.assertEqual(entry["correction"]["evidence"]["provider_units"], 90)
        self.assertEqual(entry["correction"]["approved_by"], "ops")


class ApprovalFirstVersion(unittest.TestCase):
    def test_unapproved_run_proposes_only(self):
        prov, ledger, rec = rig()
        run = rec.reconcile(local_map(minor=300),
                            provider_map(invoice_minor=360), now_ms=NOW)
        entry = run["rows"][0]
        self.assertEqual(entry["apply_status"], "proposed")
        self.assertTrue(entry["correction_id"])   # deterministic proposal
        self.assertEqual(prov.calls, {})          # zero provider writes
        self.assertEqual(ledger.ids(), [])

    def test_ledger_refuses_unapproved_correction(self):
        prov, ledger, _ = rig()
        with self.assertRaises(ValueError):
            ledger.apply(make_correction(approved_by=""), prov.credit_note)

    def test_audit_trail_recorded(self):
        _, ledger, rec = rig()
        run = rec.reconcile(local_map(minor=300),
                            provider_map(invoice_minor=360), now_ms=NOW,
                            approved_by="ops@example")
        entry = ledger.entry(run["rows"][0]["correction_id"])
        correction = entry["correction"]
        self.assertEqual(correction["approved_by"], "ops@example")  # who
        self.assertEqual(correction["approved_at_ms"], NOW)         # when
        evidence = correction["evidence"]                           # why
        self.assertEqual(evidence["invoice_minor"], 360)
        self.assertEqual(evidence["money_gap"], -60)
        self.assertEqual(evidence["provider_units"], 100)


class MutationGuards(unittest.TestCase):
    """Each safeguard must be load-bearing: break it, the invariant
    flips (CONTRIBUTING mutate-and-fail idiom)."""

    def test_judgeable_guard_pending_never_judged(self):
        pending = {"status": "pending", "units": 80, "invoice_minor": 300}
        with self.assertRaises(ReconciliationBlocked):   # guard holds
            diff({"units": 100, "minor": 300}, pending)
        with mock.patch.object(billing_reconcile, "_judgeable",
                               lambda provider: True):
            judged = diff({"units": 100, "minor": 300}, pending)
            self.assertEqual(judged["kind"], "units_mismatch")  # mutant
            # judges the PROVISIONAL pending number as a real gap

    def test_in_flight_guard_async_tolerance(self):
        args = ({"units": 100, "minor": 300},
                {"status": "ready", "units": 80, "in_flight_units": 20,
                 "invoice_minor": 300})
        self.assertEqual(diff(*args)["kind"], "match")   # guard holds
        with mock.patch.object(billing_reconcile, "_units_gap",
                               lambda l, p, i: l - p):
            self.assertEqual(diff(*args)["kind"],
                             "units_mismatch")  # mutant: spurious gap

    def test_ledger_dedupe_guard_apply_once(self):
        prov, ledger, _ = rig()
        correction = make_correction()
        calls = []

        def action(c):
            calls.append(c.correction_id)
            return prov.credit_note(c)

        ledger.apply(correction, action)
        with mock.patch.object(CorrectionsLedger, "_known",
                               lambda self, cid: False), \
             mock.patch.object(CorrectionsLedger, "_remote_receipt",
                               lambda self, c: None):
            ledger.apply(correction, action)
            self.assertEqual(len(calls), 2)   # mutant re-runs the action
        fresh = CorrectionsLedger(lambda cid: None)
        fresh_calls = []

        def action2(c):
            fresh_calls.append(c.correction_id)
            return {"provider_id": "x"}

        fresh.apply(correction, action2)
        fresh.apply(correction, action2)
        self.assertEqual(len(fresh_calls), 1)  # guard holds: applied once

    def test_recovery_guard_reads_never_writes(self):
        # guard holds on a clean rig: lost-ack recovery is a status READ
        prov2, ledger2, _ = rig()
        correction = make_correction()

        def flaky(c):
            prov2.credit_note(c)
            raise TimeoutError("ack lost")

        with self.assertRaises(TimeoutError):
            ledger2.apply(correction, flaky)   # accepted, not recorded
        receipt = ledger2.apply(correction, prov2.credit_note)
        self.assertEqual(receipt["status"], "recovered")
        self.assertEqual(prov2.calls["credit_note"], 1)   # READ only

        # mutant: recovery issues a fresh WRITE instead of the query
        prov, ledger, _ = rig()

        def flaky1(c):
            prov.credit_note(c)
            raise TimeoutError("ack lost")

        with self.assertRaises(TimeoutError):
            ledger.apply(correction, flaky1)
        with mock.patch.object(CorrectionsLedger, "_remote_receipt",
                               lambda self, c: prov.credit_note(c)):
            ledger.apply(correction, prov.credit_note)
            self.assertEqual(prov.calls["credit_note"], 2)  # mutant WRITES

    def test_deadline_guard_async_tolerance(self):
        _, _, rec = rig()
        local = local_map(units=100)
        provider = provider_map(status="pending", units=80)
        run = rec.reconcile(local, provider, now_ms=NOW,
                            ready_deadline_ms=NOW + 1000)
        self.assertEqual(run["rows"][0]["kind"],
                         "awaiting_aggregation")     # guard holds
        with mock.patch.object(BillingReconciler, "_deadline_passed",
                               lambda self, n, d: True):
            run2 = rec.reconcile(local, provider, now_ms=NOW,
                                 ready_deadline_ms=NOW + 1000)
            self.assertEqual(run2["rows"][0]["kind"],
                             "stalled")  # mutant: no tolerance window

    def test_target_guard_finalized_invoice_never_amended(self):
        _, ledger, rec = rig()
        local = local_map(minor=300)
        provider = provider_map(invoice_minor=360, invoice_state="finalized")
        run = rec.reconcile(local, provider, now_ms=NOW, approved_by="ops")
        correction = ledger.entry(run["rows"][0]["correction_id"])["correction"]
        self.assertEqual(correction["kind"], "credit_note")   # guard holds
        self.assertEqual(correction["target"], "next_invoice")
        _, ledger2, rec2 = rig()
        with mock.patch.object(BillingReconciler, "_money_target",
                               lambda self, s: ("draft_amendment",
                                                "draft_line")):
            run2 = rec2.reconcile(local, provider, now_ms=NOW,
                                  approved_by="ops")
            mutant = ledger2.entry(run2["rows"][0]["correction_id"])
            self.assertEqual(mutant["correction"]["kind"],
                             "draft_amendment")  # mutant amends FINALIZED

    def test_approval_guard_no_auto_adjustment(self):
        prov, ledger, _ = rig()
        correction = make_correction(approved_by="")
        with self.assertRaises(ValueError):   # guard holds: refused
            ledger.apply(correction, prov.credit_note)
        with mock.patch.object(CorrectionsLedger, "_approval_ok",
                               lambda self, c: True):
            ledger.apply(correction, prov.credit_note)
            self.assertEqual(ledger.ids(), [correction.correction_id])
            # mutant auto-applies an unapproved adjustment

    def test_deterministic_id_guard_rerun_no_duplicates(self):
        local, provider = local_map(minor=300), provider_map(units=90)
        prov, ledger, rec = rig()
        rec.reconcile(local, provider, now_ms=NOW, approved_by="ops")
        rec.reconcile(local, provider, now_ms=NOW, approved_by="ops")
        self.assertEqual(prov.effects, 1)   # guard holds: rerun no-op
        prov2, ledger2, rec2 = rig()
        nonce = itertools.count()
        with mock.patch.object(BillingReconciler, "_correction_id",
                               lambda self, m, w, mt, s: f"corr:{next(nonce)}"):
            rec2.reconcile(local, provider, now_ms=NOW, approved_by="ops")
            rec2.reconcile(local, provider, now_ms=NOW, approved_by="ops")
            self.assertEqual(prov2.effects, 2)  # mutant double-applies


if __name__ == "__main__":
    unittest.main()
