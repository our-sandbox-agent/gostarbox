#!/usr/bin/env python3
"""Stripe bridge contract semantics for #27 (stdlib only, NO network).

Executable semantics of the billing-bridge contract — outbox delivery,
webhook idempotence, meter-event timestamps and billing eligibility — as
an in-memory library with every remote interaction INJECTED:

- OutboxRecorder: record-pending -> send -> confirm with an injected
  transport callable and an idempotent status query. The provider dedupe
  key is the OUTBOX ID, so a remote-success/local-timeout retry or a
  crash-between-send-and-confirm replay NEVER double-bills; recovery
  re-confirms through the status query (a READ, never a fresh write).
  Same idiom as scripts/resource_events.py commit_with_outbox, lifted to
  the provider boundary.
- WebhookProcessor: injected signature verifier (constant-time compare
  REQUIRED, see hmac_verifier), apply-once-per-event-id dedupe with the
  identical receipt on redelivery, out-of-order tolerant monotonic state
  machines, and a CLOSED event-type set: unknown types are logged and
  ignored, never a crash (issue #27: do not treat three events as the
  full lifecycle).
- MeterEvent / MeteringClient: a meter event carries its OWN occurred_at
  time — last-hour-of-month usage reports with the occurred time, NEVER
  the send time (月底最後一小時 acceptance). Aggregation is asynchronous
  (issue note: cannot judge reconciliation right after send): a fresh
  report is aggregate_status 'pending' and only an injected poll can
  move it to 'ready'; reconciliation REFUSES pending aggregates.
- BillingEligibility: policy hook mapping payment state -> allowed
  actions, with 未付款處置 (non-payment disposition) explicit per state.

Real Stripe test-mode validation (units, decimals, rounding, timestamp
formats, provider dedupe windows, async aggregation latency) is a
test-mode-account checklist in docs/contracts/stripe-bridge.md. This
library makes NO network calls, holds NO keys, and live keys are never
in scope for any slice of this repo.

Tests: scripts/test_stripe_bridge.py.
Spec: docs/contracts/stripe-bridge.md.
"""
import copy
import dataclasses
import hashlib
import hmac
import json

SCHEMA_VERSION = 1


def _require_int(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {value!r}")


# ------------------------------------------------------------------ outbox
class OutboxRecorder:
    """Reliable outbox: record-pending -> send -> confirm.

    Durability model: the receipt of a send is VOLATILE until confirm
    persists it (the HTTP response may be lost in a crash — exactly the
    remote-success/local-timeout window). Therefore:

    - resend of an entry with a recorded receipt returns that receipt
      and never touches the provider (no double bill, no extra call);
    - resend after a lost receipt re-invoke the transport with the SAME
      outbox id — the provider dedupes on that id and returns the
      ORIGINAL receipt, billing once;
    - crash between send and confirm: on restart, recover() re-confirms
      through the injected idempotent status query (a READ — recovery
      must not issue a fresh write).

    The named _-prefixed helpers are deliberate mutation points for the
    guard tests (scripts/test_stripe_bridge.py), not extension points.
    """

    def __init__(self, transport, status_query):
        self._transport = transport      # callable(carrier) -> receipt (may raise TimeoutError)
        self._status_query = status_query  # callable(outbox_id) -> receipt|None, idempotent READ
        self._entries = {}
        self._seq = 0

    # ------------------------------------------------------------ steps
    def record(self, payload):
        """Persist the intention to send FIRST; delivery comes later."""
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        self._seq += 1
        oid = f"obx_{self._seq:06d}"
        entry = {"outbox_id": oid, "payload": copy.deepcopy(payload),
                 "status": "pending", "attempts": 0, "receipt": None}
        self._entries[oid] = entry
        return self._view(entry)

    def send(self, outbox_id):
        """Deliver through the injected transport (idempotent on the
        provider side via the outbox id). A transport TimeoutError is
        propagated with the entry left pending: the remote MAY have
        succeeded — the retry is safe because of the dedupe key."""
        entry = self._entry(outbox_id)
        receipt = self._already_delivered(entry)
        if receipt is not None:
            return {"outbox_id": outbox_id, "status": entry["status"],
                    "receipt": copy.deepcopy(receipt),
                    "attempts": entry["attempts"], "resent": False}
        entry["attempts"] += 1
        receipt = self._transport(self._carrier(entry))
        entry["receipt"] = receipt
        entry["status"] = "sent"
        return {"outbox_id": outbox_id, "status": "sent",
                "receipt": copy.deepcopy(receipt),
                "attempts": entry["attempts"], "resent": entry["attempts"] > 1}

    def confirm(self, outbox_id):
        """Persist the receipt as durable ('confirmed'). Idempotent; for
        an entry whose receipt was lost (crash image) the receipt is
        re-fetched through the idempotent status query."""
        entry = self._entry(outbox_id)
        if entry["status"] == "confirmed":
            return {"outbox_id": outbox_id, "status": "confirmed",
                    "receipt": copy.deepcopy(entry["receipt"]), "changed": False}
        receipt = entry["receipt"]
        if receipt is None:
            receipt = self._remote_receipt(entry)
        entry["receipt"] = receipt
        entry["status"] = "confirmed"
        return {"outbox_id": outbox_id, "status": "confirmed",
                "receipt": copy.deepcopy(receipt), "changed": True}

    def recover(self):
        """Restart replay: drive every non-confirmed entry to confirmed.
        Unsent / receipt-less entries are (re)sent — provider dedupe on
        the outbox id makes the resend bill exactly once — then
        confirmed; already-sent-but-unconfirmed entries re-confirm via
        the status query READ. Safe to call on an already-converged
        outbox (no-op)."""
        confirmed = []
        for entry in list(self._entries.values()):
            if entry["status"] == "confirmed":
                continue
            if entry["status"] == "pending":  # never sent: send first
                self.send(entry["outbox_id"])
            # 'sent' (receipt lost in the crash) re-confirms through the
            # idempotent status query — a READ, never a fresh write
            self.confirm(entry["outbox_id"])
            confirmed.append(entry["outbox_id"])
        return {"confirmed": confirmed}

    # -------------------------------------------------- mutation points
    def _carrier(self, entry):
        """What the provider sees: the dedupe key (outbox id) + body."""
        return {"outbox_id": entry["outbox_id"],
                "payload": copy.deepcopy(entry["payload"])}

    def _already_delivered(self, entry):
        """Replay short-circuit: a recorded receipt is returned as-is —
        the provider is never re-hit for an entry it already accepted."""
        return entry["receipt"] if entry["status"] in ("sent", "confirmed") else None

    def _remote_receipt(self, entry):
        """Crash-between-send-and-confirm recovery: re-fetch the receipt
        through the idempotent status query — a READ, never a fresh send
        (mutation point for the read-not-write guard)."""
        receipt = self._status_query(entry["outbox_id"])
        if receipt is None:
            raise ValueError(f"provider has no record of "
                             f"{entry['outbox_id']!r}; cannot confirm an "
                             f"entry that was never sent")
        return receipt

    # ------------------------------------------------------ introspection
    def _entry(self, outbox_id):
        entry = self._entries.get(outbox_id)
        if entry is None:
            raise ValueError(f"unknown outbox id {outbox_id!r}")
        return entry

    @staticmethod
    def _view(entry):
        return {"outbox_id": entry["outbox_id"], "status": entry["status"],
                "attempts": entry["attempts"],
                "receipt": copy.deepcopy(entry["receipt"])}

    def entry(self, outbox_id):
        return self._view(self._entry(outbox_id))

    def entries(self):
        return [self._view(e) for e in self._entries.values()]

    def pending(self):
        return [self._view(e) for e in self._entries.values()
                if e["status"] != "confirmed"]

    # -------------------------------------------------------- persistence
    def to_dict(self):
        """Crash image: unconfirmed receipts are VOLATILE and stripped —
        a snapshot taken between send and confirm carries status 'sent'
        with no receipt (the response was lost with the process)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "seq": self._seq,
            "entries": [{**e, "payload": copy.deepcopy(e["payload"]),
                         "receipt": e["receipt"] if e["status"] == "confirmed" else None}
                        for e in self._entries.values()],
        }

    @classmethod
    def from_dict(cls, data, transport, status_query):
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported persisted schema_version")
        recorder = cls(transport, status_query)
        recorder._seq = data["seq"]
        for row in data["entries"]:
            recorder._entries[row["outbox_id"]] = dict(row)
        return recorder


class ProviderDouble:
    """In-memory provider stand-in (test-mode semantics, no network).

    Bills AT MOST ONCE per idempotency key (the outbox id): a retry
    after remote-success/local-timeout returns the ORIGINAL receipt —
    the no-double-bill guarantee lives provider-side, exactly like
    Stripe idempotency keys. status() is an idempotent READ (never
    bills). timeout_sends numbers the send CALLS whose response is lost
    AFTER the remote side succeeded (remote-success-then-local-timeout).
    """

    def __init__(self, timeout_sends=()):
        self._receipts = {}
        self._timeout_sends = set(timeout_sends)
        self.billings = 0        # actual side effects — one per unique key
        self.send_calls = 0      # write attempts seen (dedupe visible)

    def send(self, carrier):
        self.send_calls += 1
        key = carrier["outbox_id"]
        if key not in self._receipts:
            self.billings += 1
            self._receipts[key] = {
                "provider_id": f"ch_{self.billings:06d}",
                "idempotency_key": key,
                "amount_minor": carrier["payload"].get("amount_minor")}
        if self.send_calls in self._timeout_sends:
            raise TimeoutError("response lost; the remote side MAY have "
                               "succeeded — retry is dedupe-safe")
        return copy.deepcopy(self._receipts[key])

    def status(self, outbox_id):
        receipt = self._receipts.get(outbox_id)
        return copy.deepcopy(receipt) if receipt is not None else None


# ----------------------------------------------------------------- webhooks
# Closed event-type set (issue #27): the five types the issue names for
# testing, plus invoice.finalized — the ordering-only lifecycle event the
# out-of-order tolerance is defined against (invoice.paid may arrive
# BEFORE invoice.finalized). Anything outside this set is logged and
# ignored, never a crash.
WEBHOOK_EVENT_TYPES = frozenset({
    "invoice.finalized",
    "invoice.paid",
    "invoice.payment_failed",
    "subscription.canceled",
    "subscription.deleted",
    "meter.error_reported",
})

# Monotonic ranks: out-of-order redelivery never DOWNGRADES a state —
# paid-before-finalized converges to paid, deleted-before-canceled
# converges to deleted, a late payment_failed never un-pays a paid
# invoice, and a retry-success paid clears an earlier payment_failed.
_INVOICE_EVENT_STATE = {"invoice.finalized": "finalized",
                        "invoice.paid": "paid",
                        "invoice.payment_failed": "payment_failed"}
_INVOICE_RANK = {"open": 0, "finalized": 1, "payment_failed": 2, "paid": 3}
_SUB_EVENT_STATE = {"subscription.canceled": "canceled",
                    "subscription.deleted": "deleted"}
_SUB_RANK = {"active": 0, "canceled": 1, "deleted": 2}


def hmac_verifier(secret):
    """Webhook signature verifier over the RAW body (Stripe-style).

    CONSTANT-TIME REQUIREMENT: any injected verifier MUST compare
    signatures with a constant-time function — hmac.compare_digest.
    A plain == leaks the expected signature bit by bit through timing.
    This built-in complies; the guard test scans for compare_digest.
    """
    if not isinstance(secret, (bytes, bytearray)):
        raise ValueError("secret must be bytes")

    def verify(body, signature):
        if not isinstance(signature, str):
            return False
        if isinstance(body, str):
            body = body.encode()
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    return verify


class WebhookProcessor:
    """Verified, deduped, out-of-order tolerant webhook intake.

    Ordering contract: verify signature -> parse -> dedupe by event id
    (redelivery returns the SAME stored receipt, no re-processing) ->
    closed-set check (unknown type logged + ignored, not a crash) ->
    apply the monotonic state transition, apply-once per event id.

    A rejected signature does NOT record the event id: a correctly
    signed redelivery of the same event must still be processable.

    The named _-prefixed helpers are deliberate mutation points for the
    guard tests, not extension points.
    """

    def __init__(self, verifier):
        self._verifier = verifier  # callable(raw_body_bytes, signature) -> bool
        self._processed = {}       # event_id -> receipt (apply-once store)
        self._invoices = {}        # invoice_id -> {state, customer, failed_at_ms}
        self._subscriptions = {}   # sub_id -> {state, customer}
        self._meter_errors = []
        self._ignored = []

    # ------------------------------------------------------------ intake
    def deliver(self, raw_body, signature):
        body = raw_body.encode() if isinstance(raw_body, str) else raw_body
        if not self._signature_ok(body, signature):
            return {"status": "rejected_signature"}
        try:
            event = json.loads(body)
        except ValueError:
            return {"status": "rejected_malformed",
                    "reason": "body is not valid JSON"}
        if (not isinstance(event, dict)
                or not isinstance(event.get("id"), str) or not event["id"]
                or not isinstance(event.get("type"), str)):
            return {"status": "rejected_malformed",
                    "reason": "event must carry a string id and type"}
        event_id, etype = event["id"], event["type"]
        if self._seen(event_id):
            return copy.deepcopy(self._processed[event_id])  # same receipt
        if etype not in WEBHOOK_EVENT_TYPES:
            receipt = {"status": "ignored_unknown_type",
                       "event_id": event_id, "type": etype}
            self._ignored.append({"event_id": event_id, "type": etype,
                                  "reason": "outside the closed event-type set"})
        else:
            receipt = self._apply(event)
        self._processed[event_id] = receipt
        return copy.deepcopy(receipt)

    # -------------------------------------------------- mutation points
    def _signature_ok(self, body, signature):
        """Verification hook (mutation point for the forgery guard)."""
        return bool(self._verifier(body, signature))

    def _seen(self, event_id):
        """Apply-once dedupe lookup (mutation point for the dedupe guard)."""
        return event_id in self._processed

    def _apply(self, event):
        etype = event["type"]
        obj = ((event.get("data") or {}).get("object") or {})
        if etype in _INVOICE_EVENT_STATE:
            return self._apply_invoice(event, obj, etype)
        if etype in _SUB_EVENT_STATE:
            return self._apply_subscription(event, obj, etype)
        return self._apply_meter_error(event, obj, etype)

    def _invoice_transition(self, record, event_state):
        """Monotonic payment-state transition: only a HIGHER rank moves
        the state (mutation point for the out-of-order guard — the mutant
        applies last-write-wins and downgrades on late events)."""
        if _INVOICE_RANK[event_state] > _INVOICE_RANK[record["state"]]:
            return event_state
        return record["state"]

    def _subscription_transition(self, record, event_state):
        if _SUB_RANK[event_state] > _SUB_RANK[record["state"]]:
            return event_state
        return record["state"]

    # ------------------------------------------------------------ apply
    def _apply_invoice(self, event, obj, etype):
        invoice_id = obj.get("id")
        if not isinstance(invoice_id, str) or not invoice_id:
            return {"status": "rejected_malformed", "event_id": event["id"],
                    "type": etype,
                    "reason": "invoice event requires data.object.id"}
        record = self._invoices.setdefault(
            invoice_id, {"state": "open", "customer": None,
                         "failed_at_ms": None})
        old = record["state"]
        record["state"] = self._invoice_transition(
            record, _INVOICE_EVENT_STATE[etype])
        if isinstance(obj.get("customer"), str):
            record["customer"] = obj["customer"]
        if etype == "invoice.payment_failed":
            record["failed_at_ms"] = event.get("created")
        return {"status": "applied", "event_id": event["id"], "type": etype,
                "invoice_id": invoice_id, "from_state": old,
                "to_state": record["state"]}

    def _apply_subscription(self, event, obj, etype):
        sub_id = obj.get("id")
        if not isinstance(sub_id, str) or not sub_id:
            return {"status": "rejected_malformed", "event_id": event["id"],
                    "type": etype,
                    "reason": "subscription event requires data.object.id"}
        record = self._subscriptions.setdefault(
            sub_id, {"state": "active", "customer": None})
        old = record["state"]
        record["state"] = self._subscription_transition(
            record, _SUB_EVENT_STATE[etype])
        if isinstance(obj.get("customer"), str):
            record["customer"] = obj["customer"]
        return {"status": "applied", "event_id": event["id"], "type": etype,
                "subscription_id": sub_id, "from_state": old,
                "to_state": record["state"]}

    def _apply_meter_error(self, event, obj, etype):
        self._meter_errors.append({
            "event_id": event["id"],
            "meter_error_id": obj.get("id"),
            "customer": obj.get("customer"),
            "error": copy.deepcopy(obj.get("error"))})
        return {"status": "applied", "event_id": event["id"], "type": etype,
                "meter_error_id": obj.get("id")}

    # ------------------------------------------------------------ views
    def customer_state(self, customer_id):
        """Derived payment state for BillingEligibility: subscription
        canceled/deleted is terminal and wins; else ANY failed invoice
        warns (conservative — dunning blocks new provisioning until the
        same invoice reaches paid via retry success); else paid; else
        open (no billing evidence yet)."""
        subs = [s for s in self._subscriptions.values()
                if s["customer"] == customer_id]
        invoices = [i for i in self._invoices.values()
                    if i["customer"] == customer_id]
        if any(s["state"] == "deleted" for s in subs):
            return "deleted"
        if any(s["state"] == "canceled" for s in subs):
            return "canceled"
        if any(i["state"] == "payment_failed" for i in invoices):
            return "payment_failed"
        if any(i["state"] == "paid" for i in invoices):
            return "paid"
        return "open"

    def last_failure(self, customer_id):
        """Latest recorded payment_failed time (grace anchor), or None."""
        times = [i["failed_at_ms"] for i in self._invoices.values()
                 if i["customer"] == customer_id
                 and i["failed_at_ms"] is not None]
        return max(times) if times else None

    def invoice_states(self):
        return copy.deepcopy(self._invoices)

    def subscription_states(self):
        return copy.deepcopy(self._subscriptions)

    def meter_errors(self):
        return copy.deepcopy(self._meter_errors)

    def ignored(self):
        return copy.deepcopy(self._ignored)

    # -------------------------------------------------------- persistence
    def to_dict(self):
        return {
            "schema_version": SCHEMA_VERSION,
            "processed": copy.deepcopy(self._processed),
            "invoices": copy.deepcopy(self._invoices),
            "subscriptions": copy.deepcopy(self._subscriptions),
            "meter_errors": copy.deepcopy(self._meter_errors),
            "ignored": copy.deepcopy(self._ignored),
        }

    @classmethod
    def from_dict(cls, verifier, data):
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported persisted schema_version")
        proc = cls(verifier)
        proc._processed = copy.deepcopy(data["processed"])
        proc._invoices = copy.deepcopy(data["invoices"])
        proc._subscriptions = copy.deepcopy(data["subscriptions"])
        proc._meter_errors = copy.deepcopy(data["meter_errors"])
        proc._ignored = copy.deepcopy(data["ignored"])
        return proc


# ----------------------------------------------------------------- metering
class ReconciliationBlocked(Exception):
    """Raised when reconciliation is attempted against a PENDING usage
    aggregate: Stripe aggregation is asynchronous — a fresh report must
    be polled to ready before any reconciliation judgment (issue #27)."""


@dataclasses.dataclass(frozen=True)
class MeterEvent:
    """A meter event carrying its OWN timestamp.

    occurred_at_ms is when the usage HAPPENED — the send time never
    replaces it: last-hour-of-month usage reports with the occurred time
    and lands on the closing month, never the next month (月底最後一小時
    acceptance, issue #27).
    """

    event_id: str
    customer_id: str
    event_name: str      # meter name, e.g. "cpu_reserved"
    value: int
    occurred_at_ms: int  # the event's own time — NOT the send time

    def __post_init__(self):
        for field in ("event_id", "customer_id", "event_name"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ValueError(f"{field} must be a non-empty string")
        _require_int(self.value, "value")
        _require_int(self.occurred_at_ms, "occurred_at_ms")


class MeteringClient:
    """Meter-event recording with ASYNC aggregation semantics.

    A freshly reported event is aggregate_status 'pending' NO MATTER that
    the transport ACKed instantly — Stripe aggregates asynchronously
    (issue #27 note: cannot judge reconciliation right after sending).
    Only an injected poll can move the aggregate to 'ready', and
    reconcile() refuses pending aggregates outright.
    """

    def __init__(self, transport, poll):
        self._transport = transport  # callable(payload) -> provider ack
        self._poll = poll            # callable(event_id) -> {"status", "value"?}
        self._records = {}           # event_id -> record

    def _payload_timestamp(self, event, now_ms):
        """The reported timestamp is the OCCURRED time, never the send
        time (mutation point for the timestamp guard)."""
        return event.occurred_at_ms

    def report(self, event, now_ms):
        """Send one meter event; dedupes on event_id (redelivery returns
        the recorded payload without a second send)."""
        _require_int(now_ms, "now_ms")
        known = self._records.get(event.event_id)
        if known is not None:
            return {"event_id": event.event_id, "status": "duplicate",
                    "aggregate_status": known["aggregate_status"],
                    "payload": copy.deepcopy(known["payload"])}
        payload = {"event_id": event.event_id,
                   "customer_id": event.customer_id,
                   "event_name": event.event_name,
                   "value": event.value,
                   "timestamp": self._payload_timestamp(event, now_ms)}
        ack = self._transport(copy.deepcopy(payload))
        self._records[event.event_id] = {
            "payload": payload, "ack": ack, "aggregate_status": "pending",
            "aggregated_value": None, "reported_at_ms": now_ms}
        return {"event_id": event.event_id, "status": "recorded",
                "aggregate_status": "pending", "payload": dict(payload)}

    def poll_status(self, event_id):
        """Poll the provider; only a 'ready' answer moves the aggregate
        forward (pending is stable — aggregation is async, not instant)."""
        record = self._record(event_id)
        if record["aggregate_status"] != "ready":
            result = self._poll(event_id) or {}
            if result.get("status") == "ready":
                record["aggregate_status"] = "ready"
                record["aggregated_value"] = result.get("value")
        return self.status(event_id)

    def _reconcileable(self, record):
        """Reconciliation gate (mutation point for the pending guard):
        only READY aggregates may be judged."""
        return record["aggregate_status"] == "ready"

    def reconcile(self, event_id, expected_value):
        """Compare the provider aggregate against the local expectation.
        REFUSES pending aggregates: async aggregation means the provider
        value is not final right after the send."""
        _require_int(expected_value, "expected_value")
        record = self._record(event_id)
        if not self._reconcileable(record):
            raise ReconciliationBlocked(
                "usage aggregate is still pending; Stripe aggregation is "
                "asynchronous — poll to ready before reconciling")
        aggregated = record["aggregated_value"]
        return {"event_id": event_id, "status": "ready",
                "aggregated_value": aggregated,
                "expected_value": expected_value,
                "match": aggregated == expected_value}

    def _record(self, event_id):
        record = self._records.get(event_id)
        if record is None:
            raise ValueError(f"unknown meter event {event_id!r}")
        return record

    def aggregate_status(self, event_id):
        return self._record(event_id)["aggregate_status"]

    def status(self, event_id):
        record = self._record(event_id)
        return {"event_id": event_id,
                "aggregate_status": record["aggregate_status"],
                "aggregated_value": record["aggregated_value"]}

    def payload(self, event_id):
        return copy.deepcopy(self._record(event_id)["payload"])


# --------------------------------------------------------------- eligibility
PAYMENT_STATES = ("open", "paid", "payment_failed", "canceled", "deleted")
ACTIONS = ("provision", "resume", "continue", "destroy")
DEFAULT_GRACE_MS = 7 * 24 * 3600 * 1000  # 7-day dunning grace window

# 未付款處置 (explicit disposition per payment state)
DISPOSITIONS = {
    "open": "no billing evidence yet: trial behavior only (試用估價); "
            "eligibility decisions wait for the first invoice event",
    "paid": "fully paid: full service",
    "payment_failed": "未付款處置: WARN during the grace window — existing "
                      "workloads continue, NEW provisioning blocked; at "
                      "grace expiry escalate to suspend (stop scheduling, "
                      "RETAIN data and volumes; settlement or cancellation "
                      "resolves; never auto-destroy on payment failure)",
    "canceled": "subscription canceled: all lifecycle actions blocked; data "
                "export allowed during the retention window, then purge per "
                "retention policy",
    "deleted": "subscription deleted (terminal): nothing may be provisioned "
               "or resumed",
}


class BillingEligibility:
    """Policy hook: payment state -> allowed/blocked actions.

    paid -> eligible (all actions); payment_failed -> warn + grace config
    (existing continues, new provisioning blocked) escalating to blocked
    suspend at grace expiry; canceled/deleted -> blocked. Inject an
    object with the same evaluate() signature to override the policy —
    this class is the default hook, not a hardwired rule.
    """

    def __init__(self, grace_ms=DEFAULT_GRACE_MS):
        _require_int(grace_ms, "grace_ms", minimum=1)
        self._grace_ms = grace_ms

    def _grace_expired(self, failed_at_ms, now_ms):
        """Grace window check (mutation point for the escalation guard):
        an unknown failure time never expires on its own (warn holds
        until a real anchor arrives)."""
        if failed_at_ms is None:
            return False
        return now_ms >= failed_at_ms + self._grace_ms

    def evaluate(self, payment_state, failed_at_ms=None, now_ms=0):
        if payment_state not in PAYMENT_STATES:
            raise ValueError(f"payment_state must be one of {PAYMENT_STATES}, "
                             f"got {payment_state!r}")
        _require_int(now_ms, "now_ms")
        if failed_at_ms is not None:
            _require_int(failed_at_ms, "failed_at_ms")
        base = {"payment_state": payment_state,
                "disposition": DISPOSITIONS[payment_state]}
        if payment_state == "paid":
            return {**base, "level": "eligible", "allowed": list(ACTIONS),
                    "blocked": [], "grace": None}
        if payment_state == "payment_failed":
            expired = self._grace_expired(failed_at_ms, now_ms)
            grace = {"grace_ms": self._grace_ms,
                     "expired": expired,
                     "deadline_ms": (None if failed_at_ms is None
                                     else failed_at_ms + self._grace_ms)}
            if expired:  # escalate: suspend disposition, teardown only
                return {**base, "level": "blocked",
                        "allowed": ["destroy"],
                        "blocked": ["continue", "provision", "resume"],
                        "grace": grace}
            return {**base, "level": "warn",
                    "allowed": ["continue", "destroy", "resume"],
                    "blocked": ["provision"], "grace": grace}
        if payment_state == "open":
            return {**base, "level": "warn",
                    "allowed": ["continue", "destroy"],
                    "blocked": ["provision", "resume"], "grace": None}
        # canceled / deleted: blocked terminal
        return {**base, "level": "blocked", "allowed": [],
                "blocked": list(ACTIONS), "grace": None}
