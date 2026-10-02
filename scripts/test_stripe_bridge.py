"""Fixtures and guard tests for the #27 Stripe bridge semantics slice.

Coverage: outbox crash-point matrix (after-record / before-send /
after-send / before-confirm x replay -> EXACTLY ONE provider receipt, no
double bill), remote-success-then-local-timeout replay returning the
recorded receipt, webhook bad-signature rejection (constant-time note),
duplicate delivery apply-once, out-of-order set convergence (paid before
finalized etc.), unknown event type logged-not-crash, meter occurred_at
timestamp rule (月底最後一小時 — occurred time, never send time), pending
aggregates blocking reconciliation, the billing eligibility matrix (含
未付款處置), and mutation guards proving each safeguard load-bearing
(pattern per scripts/test_resource_events.py, CONTRIBUTING
mutate-and-fail idiom).
"""
import calendar
import hashlib
import hmac
import inspect
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import stripe_bridge  # noqa: E402
from stripe_bridge import (BillingEligibility, MeterEvent,  # noqa: E402
                           MeteringClient, OutboxRecorder, ProviderDouble,
                           ReconciliationBlocked, WebhookProcessor,
                           hmac_verifier)

SECRET = b"whsec_test_mode_only_never_live"

OCT_31_2330 = calendar.timegm((2026, 10, 31, 23, 30, 0)) * 1000
NOV_1_0005 = calendar.timegm((2026, 11, 1, 0, 5, 0)) * 1000

CRASH_POINTS = ("after_record", "before_send", "after_send", "before_confirm")


def month_bucket(ms):
    dt = datetime.fromtimestamp(ms // 1000, timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def make_processor():
    return WebhookProcessor(hmac_verifier(SECRET))


def good_signature(body):
    return hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()


def deliver(proc, event, signature=None):
    body = json.dumps(event, sort_keys=True, separators=(",", ":"))
    return proc.deliver(body, signature if signature is not None
                        else good_signature(body))


def invoice_event(event_id, etype, invoice_id="in_1", customer="cus_1",
                  created=1000):
    return {"id": event_id, "type": etype, "created": created,
            "data": {"object": {"id": invoice_id, "customer": customer}}}


def sub_event(event_id, etype, sub_id="sub_1", customer="cus_1",
              created=1000):
    return {"id": event_id, "type": etype, "created": created,
            "data": {"object": {"id": sub_id, "customer": customer}}}


def meter_error_event(event_id, customer="cus_1"):
    return {"id": event_id, "type": "meter.error_reported", "created": 1000,
            "data": {"object": {"id": "merr_1", "customer": customer,
                                "error": {"code": "invalid_timestamp"}}}}


def outbox_pipeline(provider, crash_at=None):
    """record -> [after_record | before_send] send -> [after_send |
    before_confirm] confirm. Returns (recorder, crash snapshot | None).
    after_record/before_send share a durable image (nothing sent yet);
    after_send/before_confirm share one (sent, receipt volatile)."""
    recorder = OutboxRecorder(provider.send, provider.status)
    entry = recorder.record({"topic": "invoice.attempt",
                             "amount_minor": 4200})
    if crash_at in ("after_record", "before_send"):
        return recorder, recorder.to_dict()
    recorder.send(entry["outbox_id"])
    if crash_at in ("after_send", "before_confirm"):
        return recorder, recorder.to_dict()
    recorder.confirm(entry["outbox_id"])
    return recorder, None


def meter_client(poll_result):
    captured = []

    def transport(payload):
        captured.append(payload)
        return {"accepted": True}

    client = MeteringClient(transport, lambda event_id: poll_result)
    return client, captured


def last_hour_event():
    return MeterEvent(event_id="mev_last_hour", customer_id="cus_1",
                      event_name="cpu_reserved", value=5,
                      occurred_at_ms=OCT_31_2330)


class OutboxCrashMatrix(unittest.TestCase):
    def test_each_crash_point_replays_to_exactly_one_billing(self):
        for point in CRASH_POINTS:
            with self.subTest(point=point):
                provider = ProviderDouble()
                _, snapshot = outbox_pipeline(provider, point)
                recovered = OutboxRecorder.from_dict(
                    snapshot, provider.send, provider.status)
                recovered.recover()
                self.assertEqual(provider.billings, 1)   # exactly one bill
                entry = recovered.entries()[0]
                self.assertEqual(entry["status"], "confirmed")
                self.assertEqual(entry["receipt"],
                                 provider.status(entry["outbox_id"]))

    def test_recovered_matches_clean_run(self):
        provider = ProviderDouble()
        clean, _ = outbox_pipeline(provider)
        clean_view = clean.entries()
        for point in CRASH_POINTS:
            with self.subTest(point=point):
                crashed_provider = ProviderDouble()
                _, snapshot = outbox_pipeline(crashed_provider, point)
                recovered = OutboxRecorder.from_dict(
                    snapshot, crashed_provider.send, crashed_provider.status)
                recovered.recover()
                self.assertEqual(recovered.entries(), clean_view)

    def test_recover_is_idempotent(self):
        provider = ProviderDouble()
        _, snapshot = outbox_pipeline(provider, "after_send")
        recovered = OutboxRecorder.from_dict(
            snapshot, provider.send, provider.status)
        first = recovered.recover()
        self.assertEqual(len(first["confirmed"]), 1)
        second = recovered.recover()  # already converged: no-op
        self.assertEqual(second, {"confirmed": []})
        self.assertEqual(provider.billings, 1)

    def test_after_send_recovery_confirms_via_read_not_write(self):
        provider = ProviderDouble()
        _, snapshot = outbox_pipeline(provider, "after_send")
        self.assertEqual(provider.send_calls, 1)  # the pre-crash send
        recovered = OutboxRecorder.from_dict(
            snapshot, provider.send, provider.status)
        recovered.recover()
        self.assertEqual(provider.send_calls, 1)  # status query only
        self.assertEqual(provider.billings, 1)

    def test_remote_success_local_timeout_replay_no_double_bill(self):
        provider = ProviderDouble(timeout_sends={1})
        recorder = OutboxRecorder(provider.send, provider.status)
        entry = recorder.record({"topic": "invoice.attempt",
                                 "amount_minor": 4200})
        with self.assertRaises(TimeoutError):
            recorder.send(entry["outbox_id"])   # remote billed, response lost
        self.assertEqual(recorder.entry(entry["outbox_id"])["status"],
                         "pending")
        replay = recorder.send(entry["outbox_id"])  # resend: same outbox id
        self.assertEqual(provider.billings, 1)      # dedupe: billed ONCE
        self.assertTrue(replay["resent"])
        recorder.confirm(entry["outbox_id"])
        self.assertEqual(recorder.entry(entry["outbox_id"])["receipt"],
                         provider.status(entry["outbox_id"]))

    def test_confirmed_resend_returns_recorded_receipt_no_transport(self):
        provider = ProviderDouble()
        recorder = OutboxRecorder(provider.send, provider.status)
        entry = recorder.record({"topic": "invoice.attempt"})
        recorder.send(entry["outbox_id"])
        confirmed = recorder.confirm(entry["outbox_id"])
        calls = provider.send_calls
        replay = recorder.send(entry["outbox_id"])
        self.assertEqual(provider.send_calls, calls)   # provider not re-hit
        self.assertEqual(replay["receipt"], confirmed["receipt"])
        self.assertEqual(replay["status"], "confirmed")
        self.assertFalse(replay["resent"])
        self.assertEqual(provider.billings, 1)

    def test_confirm_never_sent_entry_without_provider_record_refused(self):
        provider = ProviderDouble()
        recorder = OutboxRecorder(provider.send, provider.status)
        entry = recorder.record({"topic": "invoice.attempt"})
        with self.assertRaises(ValueError):
            recorder.confirm(entry["outbox_id"])  # nothing to re-confirm


class WebhookSignature(unittest.TestCase):
    def test_bad_signature_rejected_and_not_locked_out(self):
        proc = make_processor()
        event = invoice_event("evt_1", "invoice.paid")
        body = json.dumps(event, sort_keys=True, separators=(",", ":"))
        receipt = proc.deliver(body, "0" * 64)
        self.assertEqual(receipt["status"], "rejected_signature")
        self.assertEqual(proc.invoice_states(), {})  # nothing applied
        # a rejected signature must NOT record the event id: the same
        # event, correctly signed, is still processable
        good = deliver(proc, event)
        self.assertEqual(good["status"], "applied")

    def test_tampered_body_rejected(self):
        proc = make_processor()
        original = json.dumps(invoice_event("evt_1", "invoice.paid"),
                              sort_keys=True, separators=(",", ":"))
        signature = good_signature(original)
        tampered = original.replace("in_1", "in_9")
        self.assertEqual(proc.deliver(tampered, signature)["status"],
                         "rejected_signature")

    def test_builtin_verifier_uses_constant_time_compare(self):
        # constant-time note made load-bearing: dropping compare_digest
        # for a plain == leaks the signature through timing
        source = inspect.getsource(stripe_bridge.hmac_verifier)
        self.assertIn("compare_digest", source)


class WebhookDedupe(unittest.TestCase):
    def test_duplicate_delivery_same_receipt_once_only(self):
        proc = make_processor()
        event = meter_error_event("evt_m")
        first = deliver(proc, event)
        second = deliver(proc, event)  # redelivery
        self.assertEqual(first, second)            # same stored receipt
        self.assertEqual(second["status"], "applied")
        self.assertEqual(len(proc.meter_errors()), 1)  # applied ONCE

    def test_dedupe_survives_restart(self):
        proc = make_processor()
        deliver(proc, invoice_event("evt_1", "invoice.paid"))
        snapshot = proc.to_dict()
        restored = WebhookProcessor.from_dict(hmac_verifier(SECRET), snapshot)
        receipt = deliver(restored, invoice_event("evt_1", "invoice.paid"))
        self.assertEqual(receipt["status"], "applied")  # same receipt back
        self.assertEqual(list(restored.invoice_states()),
                         list(proc.invoice_states()))


class WebhookOutOfOrder(unittest.TestCase):
    def test_paid_before_finalized_converges(self):
        forward = make_processor()
        deliver(forward, invoice_event("evt_f", "invoice.finalized"))
        deliver(forward, invoice_event("evt_p", "invoice.paid"))
        backward = make_processor()  # paid arrives BEFORE finalized
        deliver(backward, invoice_event("evt_p", "invoice.paid"))
        deliver(backward, invoice_event("evt_f", "invoice.finalized"))
        self.assertEqual(forward.invoice_states(), backward.invoice_states())
        self.assertEqual(backward.invoice_states()["in_1"]["state"], "paid")

    def test_failed_then_paid_retry_succeeds(self):
        proc = make_processor()
        deliver(proc, invoice_event("evt_fail", "invoice.payment_failed",
                                    created=1000))
        self.assertEqual(proc.invoice_states()["in_1"]["state"],
                         "payment_failed")
        deliver(proc, invoice_event("evt_ok", "invoice.paid", created=2000))
        self.assertEqual(proc.invoice_states()["in_1"]["state"], "paid")
        self.assertEqual(proc.customer_state("cus_1"), "paid")

    def test_late_failed_never_unpays(self):
        proc = make_processor()
        deliver(proc, invoice_event("evt_p", "invoice.paid"))
        deliver(proc, invoice_event("evt_fail", "invoice.payment_failed"))
        self.assertEqual(proc.invoice_states()["in_1"]["state"], "paid")

    def test_deleted_before_canceled(self):
        proc = make_processor()
        deliver(proc, sub_event("evt_d", "subscription.deleted"))
        deliver(proc, sub_event("evt_c", "subscription.canceled"))
        self.assertEqual(proc.subscription_states()["sub_1"]["state"],
                         "deleted")
        self.assertEqual(proc.customer_state("cus_1"), "deleted")

    def test_full_set_permutations_converge(self):
        events = [
            invoice_event("evt_f", "invoice.finalized", "in_1"),
            invoice_event("evt_p", "invoice.paid", "in_1"),
            invoice_event("evt_x", "invoice.payment_failed", "in_2"),
            sub_event("evt_c", "subscription.canceled"),
            meter_error_event("evt_m"),
        ]
        views = []
        for order in ([0, 1, 2, 3, 4], [1, 0, 3, 4, 2],
                      [3, 1, 4, 0, 2], [2, 4, 1, 3, 0]):
            with self.subTest(order=order):
                proc = make_processor()
                for index in order:
                    receipt = deliver(proc, events[index])
                    self.assertEqual(receipt["status"], "applied")
                views.append((proc.invoice_states(),
                              proc.subscription_states(),
                              len(proc.meter_errors()),
                              proc.customer_state("cus_1")))
        self.assertEqual(views[0][0]["in_1"]["state"], "paid")
        self.assertEqual(views[0][0]["in_2"]["state"], "payment_failed")
        self.assertTrue(all(view == (views[0][0], views[0][1], 1, "canceled")
                            for view in views))


class WebhookUnknownType(unittest.TestCase):
    def test_unknown_type_logged_ignored_not_crash(self):
        proc = make_processor()
        receipt = deliver(proc, {"id": "evt_u", "type": "charge.dispute.created",
                                 "created": 1000,
                                 "data": {"object": {"id": "dp_1"}}})
        self.assertEqual(receipt["status"], "ignored_unknown_type")
        self.assertEqual(len(proc.ignored()), 1)   # logged...
        self.assertEqual(proc.invoice_states(), {})  # ...not applied
        self.assertEqual(proc.subscription_states(), {})

    def test_unknown_then_known_processes(self):
        proc = make_processor()
        deliver(proc, {"id": "evt_u", "type": "customer.created",
                       "data": {"object": {"id": "cus_9"}}})
        receipt = deliver(proc, invoice_event("evt_p", "invoice.paid"))
        self.assertEqual(receipt["status"], "applied")

    def test_malformed_events_rejected_not_recorded(self):
        proc = make_processor()
        body = "not json at all"
        self.assertEqual(proc.deliver(body, good_signature(body))["status"],
                         "rejected_malformed")
        no_id = json.dumps({"type": "invoice.paid", "data": {}})
        self.assertEqual(proc.deliver(no_id, good_signature(no_id))["status"],
                         "rejected_malformed")
        self.assertEqual(proc.ignored(), [])
        self.assertEqual(proc.invoice_states(), {})


class MeterTimestampRule(unittest.TestCase):
    def test_occurred_at_used_never_send_time(self):
        client, captured = meter_client({"status": "pending"})
        # last hour of October, sent five minutes into November
        client.report(last_hour_event(), now_ms=NOV_1_0005)
        payload = captured[0]
        self.assertEqual(payload["timestamp"], OCT_31_2330)  # occurred time
        self.assertNotEqual(payload["timestamp"], NOV_1_0005)
        self.assertEqual(month_bucket(payload["timestamp"]), "2026-10")
        self.assertNotEqual(month_bucket(payload["timestamp"]),
                            month_bucket(NOV_1_0005))

    def test_report_idempotent_same_event_id(self):
        client, captured = meter_client({"status": "pending"})
        client.report(last_hour_event(), now_ms=NOV_1_0005)
        duplicate = client.report(last_hour_event(), now_ms=NOV_1_0005 + 60_000)
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(len(captured), 1)  # no second send
        self.assertEqual(duplicate["payload"]["timestamp"], OCT_31_2330)

    def test_meter_event_validation(self):
        def assert_invalid(*args):
            with self.assertRaises(ValueError):
                MeterEvent(*args)

        assert_invalid("", "cus_1", "cpu_reserved", 5, 0)   # empty event_id
        assert_invalid("mev", "cus_1", "cpu_reserved", -1, 0)  # negative value
        assert_invalid("mev", "cus_1", "cpu_reserved", 5, -1)  # negative time
        assert_invalid("mev", "cus_1", "cpu_reserved", 1.5, 0)  # float value


class MeterAggregation(unittest.TestCase):
    def _reported(self, poll_result):
        client, _ = meter_client(poll_result)
        client.report(last_hour_event(), now_ms=NOV_1_0005)
        return client

    def test_pending_immediately_after_report(self):
        # async aggregation: pending NO MATTER that the send ACKed
        client = self._reported({"status": "ready", "value": 5})
        self.assertEqual(client.aggregate_status("mev_last_hour"), "pending")

    def test_reconcile_blocked_while_pending(self):
        client = self._reported({"status": "pending"})
        with self.assertRaises(ReconciliationBlocked):
            client.reconcile("mev_last_hour", 5)

    def test_poll_pending_then_ready_flow(self):
        client, _ = meter_client({"status": "pending"})
        client.report(last_hour_event(), now_ms=NOV_1_0005)
        self.assertEqual(client.poll_status("mev_last_hour")
                         ["aggregate_status"], "pending")
        with self.assertRaises(ReconciliationBlocked):  # still not judgeable
            client.reconcile("mev_last_hour", 5)
        # only a ready poll flips the gate; then reconcile works
        client._poll = lambda event_id: {"status": "ready", "value": 5}
        status = client.poll_status("mev_last_hour")
        self.assertEqual(status["aggregate_status"], "ready")
        self.assertEqual(status["aggregated_value"], 5)
        self.assertTrue(client.reconcile("mev_last_hour", 5)["match"])
        self.assertFalse(client.reconcile("mev_last_hour", 6)["match"])

    def test_reconcile_unknown_event_raises(self):
        client = self._reported({"status": "ready", "value": 5})
        with self.assertRaises(ValueError):
            client.reconcile("mev_missing", 5)


class EligibilityMatrix(unittest.TestCase):
    def test_matrix(self):
        elig = BillingEligibility()
        cases = {
            "paid": ("eligible", list(stripe_bridge.ACTIONS), []),
            "open": ("warn", ["continue", "destroy"],
                     ["provision", "resume"]),
            "canceled": ("blocked", [], list(stripe_bridge.ACTIONS)),
            "deleted": ("blocked", [], list(stripe_bridge.ACTIONS)),
        }
        for state, (level, allowed, blocked) in cases.items():
            with self.subTest(state=state):
                result = elig.evaluate(state)
                self.assertEqual(result["level"], level)
                self.assertEqual(sorted(result["allowed"]), sorted(allowed))
                self.assertEqual(sorted(result["blocked"]), sorted(blocked))
                self.assertTrue(result["disposition"])  # 處置 explicit

    def test_payment_failed_within_grace_warns(self):
        elig = BillingEligibility(grace_ms=1000)
        result = elig.evaluate("payment_failed", failed_at_ms=0, now_ms=999)
        self.assertEqual(result["level"], "warn")
        self.assertEqual(result["blocked"], ["provision"])  # new spend off
        self.assertEqual(sorted(result["allowed"]),
                         ["continue", "destroy", "resume"])
        self.assertEqual(result["grace"]["deadline_ms"], 1000)
        self.assertFalse(result["grace"]["expired"])

    def test_grace_expiry_escalates_to_blocked_suspend(self):
        elig = BillingEligibility(grace_ms=1000)
        result = elig.evaluate("payment_failed", failed_at_ms=0, now_ms=1000)
        self.assertEqual(result["level"], "blocked")
        self.assertTrue(result["grace"]["expired"])
        self.assertEqual(result["allowed"], ["destroy"])  # teardown only
        self.assertIn("suspend", result["disposition"])  # 未付款處置
        self.assertIn("never auto-destroy", result["disposition"])

    def test_unknown_state_rejected(self):
        with self.assertRaises(ValueError):
            BillingEligibility().evaluate("past_due")

    def test_webhook_customer_state_feeds_eligibility(self):
        proc = make_processor()
        deliver(proc, invoice_event("evt_fail", "invoice.payment_failed",
                                    created=5000))
        elig = BillingEligibility()
        result = elig.evaluate(proc.customer_state("cus_1"),
                               failed_at_ms=proc.last_failure("cus_1"),
                               now_ms=5001)
        self.assertEqual(result["level"], "warn")  # grace window open


class MutationGuards(unittest.TestCase):
    """Each safeguard must be load-bearing: break it, the invariant
    flips (CONTRIBUTING mutate-and-fail idiom)."""

    def test_timestamp_guard_occurred_not_send_time(self):
        client, captured = meter_client({"status": "pending"})
        with mock.patch.object(MeteringClient, "_payload_timestamp",
                               lambda self, event, now_ms: now_ms):
            client.report(last_hour_event(), now_ms=NOV_1_0005)
        # mutant ships the send time: last-hour usage lands in November
        self.assertEqual(captured[0]["timestamp"], NOV_1_0005)
        self.assertEqual(month_bucket(captured[0]["timestamp"]), "2026-11")

    def test_pending_aggregate_guard_blocks_reconciliation(self):
        client, _ = meter_client({"status": "pending"})
        client.report(last_hour_event(), now_ms=NOV_1_0005)
        with self.assertRaises(ReconciliationBlocked):  # guard holds
            client.reconcile("mev_last_hour", 5)
        with mock.patch.object(MeteringClient, "_reconcileable",
                               lambda self, record: True):
            judged = client.reconcile("mev_last_hour", 5)
            self.assertFalse(judged["match"])  # mutant judges pending data

    def test_signature_guard_rejects_forgery(self):
        proc = make_processor()
        event = invoice_event("evt_1", "invoice.paid")
        body = json.dumps(event, sort_keys=True, separators=(",", ":"))
        self.assertEqual(proc.deliver(body, "forged")["status"],
                         "rejected_signature")  # guard holds
        with mock.patch.object(WebhookProcessor, "_signature_ok",
                               lambda self, body, sig: True):
            accepted = proc.deliver(body, "forged")
            self.assertEqual(accepted["status"], "applied")  # mutant flips

    def test_dedupe_guard_prevents_reprocessing(self):
        proc = make_processor()
        event = meter_error_event("evt_m")
        deliver(proc, event)
        with mock.patch.object(WebhookProcessor, "_seen",
                               lambda self, event_id: False):
            deliver(proc, event)
            self.assertEqual(len(proc.meter_errors()), 2)  # re-processed
        fresh = make_processor()   # guard holds: one application only
        deliver(fresh, event)
        self.assertEqual(len(fresh.meter_errors()), 1)

    def test_out_of_order_guard_never_downgrades(self):
        proc = make_processor()
        deliver(proc, invoice_event("evt_p", "invoice.paid"))
        deliver(proc, invoice_event("evt_fail", "invoice.payment_failed"))
        self.assertEqual(proc.invoice_states()["in_1"]["state"], "paid")
        with mock.patch.object(WebhookProcessor, "_invoice_transition",
                               lambda self, record, state: state):
            mutant = make_processor()
            deliver(mutant, invoice_event("evt_p", "invoice.paid"))
            deliver(mutant, invoice_event("evt_fail",
                                          "invoice.payment_failed"))
            self.assertEqual(mutant.invoice_states()["in_1"]["state"],
                             "payment_failed")  # last-write-wins flips it

    def test_resend_guard_returns_recorded_receipt(self):
        provider = ProviderDouble()
        recorder = OutboxRecorder(provider.send, provider.status)
        entry = recorder.record({"topic": "invoice.attempt"})
        recorder.send(entry["outbox_id"])
        recorder.confirm(entry["outbox_id"])
        calls = provider.send_calls
        recorder.send(entry["outbox_id"])  # guard holds: no transport hit
        self.assertEqual(provider.send_calls, calls)
        with mock.patch.object(OutboxRecorder, "_already_delivered",
                               lambda self, entry: None):
            recorder.send(entry["outbox_id"])
            self.assertEqual(provider.send_calls, calls + 1)  # mutant re-hits
            # provider dedupe still saves the bill — but the bridge no
            # longer guarantees it for providers with expiring windows
            self.assertEqual(provider.billings, 1)

    def test_closed_set_guard_routes_known_types(self):
        proc = make_processor()
        receipt = deliver(proc, invoice_event("evt_p", "invoice.paid"))
        self.assertEqual(receipt["status"], "applied")  # guard holds
        with mock.patch.object(stripe_bridge, "WEBHOOK_EVENT_TYPES",
                               frozenset()):
            empty = deliver(proc, invoice_event("evt_q", "invoice.paid"))
            self.assertEqual(empty["status"],
                             "ignored_unknown_type")  # mutant drops all

    def test_grace_guard_escalates_on_expiry(self):
        elig = BillingEligibility(grace_ms=1000)
        self.assertEqual(
            elig.evaluate("payment_failed", 0, 5000)["level"], "blocked")
        with mock.patch.object(BillingEligibility, "_grace_expired",
                               lambda self, failed_at_ms, now_ms: False):
            self.assertEqual(
                elig.evaluate("payment_failed", 0, 5000)["level"],
                "warn")  # mutant never escalates: dunning forever

    def test_recovery_guard_reads_never_writes(self):
        provider = ProviderDouble()
        _, snapshot = outbox_pipeline(provider, "after_send")
        recovered = OutboxRecorder.from_dict(
            snapshot, provider.send, provider.status)
        recovered.recover()
        self.assertEqual(provider.send_calls, 1)  # guard holds: READ only
        provider2 = ProviderDouble()
        _, snapshot2 = outbox_pipeline(provider2, "after_send")
        mutant = OutboxRecorder.from_dict(
            snapshot2, provider2.send, provider2.status)
        with mock.patch.object(OutboxRecorder, "_remote_receipt",
                               lambda self, entry: self._transport(
                                   self._carrier(entry))):
            mutant.recover()
            self.assertEqual(provider2.send_calls, 2)  # mutant WRITES


if __name__ == "__main__":
    unittest.main()
