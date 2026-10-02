#!/usr/bin/env python3
"""Minimal resource-event ledger slice for #77 (stdlib only, no Runner).

Implements the append/validate/persist semantics of docs/adr/usage-ledger.md
sections 1-3 as an in-memory library: event envelope validation, redelivery
dedupe, conflict isolation, per-resource monotonic ledger_seq, half-open
confirmed interval projection with integer quantity*ms totals, Lost
uncertainty gaps (H->R) that are never counted as Active nor zero, correction
with both versions kept, and state+outbox commits under one sequence id.

No rates, no currency: summarize() reports quantity*ms and uncertain ranges
only (no approved rate cards exist yet; #25 owns billing).

Real Runner integration is blocked on #11; the control plane (#76) persists
these events in the same DB transaction as state confirmations. This library
is the executable semantics of that contract, not a runtime.

Tests: scripts/test_resource_events.py.
"""
import copy
import json

SCHEMA_VERSION = 1
CERTAINTIES = ("confirmed", "uncertain")

EVENT_TYPES = frozenset({
    "operation.requested", "operation.succeeded", "operation.failed",
    "runtime.started", "runtime.stopped",
    "policy.applied",
    "resource.provisioned", "resource.resized", "resource.released",
    "heartbeat.confirmed", "lease.expired",
    "snapshot.created", "snapshot.expired", "snapshot.deleted",
    "correction.accepted",
})

# projection semantics per usage-ledger #2:
_SWITCH = frozenset({"runtime.started", "policy.applied", "resource.provisioned",
                     "resource.resized", "snapshot.created"})  # close old, open new
_CLOSE = frozenset({"runtime.stopped", "resource.released", "snapshot.deleted"})
# audit-only (no usage intervals): operation.*, heartbeat.confirmed,
# snapshot.expired (TTL expiry is a delete REQUEST, not a release),
# correction.accepted (replacement events drive the recompute).

REQUIRED_FIELDS = ("schema_version", "event_id", "tenant_id", "workspace_id",
                   "resource_type", "resource_id", "source_id", "source_seq",
                   "effective_at_ms", "type", "reason", "payload")

# fields assigned by the control plane, excluded from content comparison
ASSIGNED_FIELDS = frozenset({"ledger_seq", "recorded_at_ms"})


def _require_int(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {value!r}")


def resource_key(event):
    return f"{event['resource_type']}/{event['resource_id']}"


class EventLedger:
    """Append-only event ledger with interval projection per resource.

    The named _-prefixed methods are deliberate mutation points for the guard
    tests (scripts/test_resource_events.py), not extension points.
    """

    def __init__(self):
        self._events = []                 # append order, full envelopes
        self._by_event_id = {}
        self._by_source_key = {}          # (source_id, generation, source_seq)
        self._by_resource = {}            # rkey -> [events]
        self._ledger_seq = {}             # rkey -> last assigned ledger_seq
        self._current = {}                # rkey -> {"segments": [...], "open": ...}
        self._archive = {}                # rkey -> segments superseded by later evidence
        self._quarantine = []
        self._flags = []
        self._outbox = []
        self._outbox_by_event = {}
        self._commit_seq = 0
        self._counter = 0
        self._clock = 0                   # stand-in for DB receive time

    # ------------------------------------------------------------ append
    def append(self, event):
        event = dict(event)
        self._validate(event)
        status, stored = self._dedupe_check(event)
        if status == "duplicate":
            return {"status": "duplicate", "event_id": stored["event_id"],
                    "ledger_seq": stored["ledger_seq"],
                    "recorded_at_ms": stored["recorded_at_ms"]}
        if status == "conflict":
            return self._quarantine_event(event, stored)
        self._check_ledger_seq(event)
        rkey = resource_key(event)
        event["ledger_seq"] = self._ledger_seq.get(rkey, 0) + 1
        if "recorded_at_ms" not in event:
            event["recorded_at_ms"] = self._clock
        self._ledger_seq[rkey] = event["ledger_seq"]
        self._events.append(event)
        self._by_event_id[event["event_id"]] = event
        key = (event["source_id"], event.get("generation"), event["source_seq"])
        self._by_source_key[key] = event
        self._by_resource.setdefault(rkey, []).append(event)
        self._apply(rkey)
        return {"status": "appended", "event_id": event["event_id"],
                "ledger_seq": event["ledger_seq"],
                "recorded_at_ms": event["recorded_at_ms"]}

    def _validate(self, event):
        if not isinstance(event, dict):
            raise ValueError("event must be a dict")
        for field in REQUIRED_FIELDS:
            if field not in event:
                raise ValueError(f"missing required field {field}")
        if event["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        if event["type"] not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event['type']!r}")
        if "certainty" in event and event["certainty"] not in CERTAINTIES:
            raise ValueError("certainty must be confirmed or uncertain")
        _require_int(event["source_seq"], "source_seq")
        _require_int(event["effective_at_ms"], "effective_at_ms")
        if "recorded_at_ms" in event:
            _require_int(event["recorded_at_ms"], "recorded_at_ms")
        if "generation" in event:
            _require_int(event["generation"], "generation")
        if not isinstance(event["payload"], dict):
            raise ValueError("payload must be a dict")
        quantity = event["payload"].get("quantity")
        if quantity is not None:
            if not isinstance(quantity, dict):
                raise ValueError("payload.quantity must be a dict of meter -> integer")
            for meter, value in quantity.items():
                _require_int(value, f"payload.quantity[{meter!r}]")
        if "ledger_seq" in event:
            _require_int(event["ledger_seq"], "ledger_seq")

    def _check_ledger_seq(self, event):
        """New events only: the caller-supplied ledger_seq (if any) must be
        the next per-resource value; redelivered duplicates return earlier."""
        if "ledger_seq" not in event:
            return
        expected = self._ledger_seq.get(resource_key(event), 0) + 1
        if event["ledger_seq"] != expected:
            raise ValueError(f"ledger_seq must be the next per-resource value "
                             f"{expected}, got {event['ledger_seq']}")

    def _dedupe_check(self, event):
        """Return ('new', None) | ('duplicate', stored) | ('conflict', stored)."""
        stored = self._by_event_id.get(event["event_id"])
        if stored is None:
            key = (event["source_id"], event.get("generation"), event["source_seq"])
            stored = self._by_source_key.get(key)
        if stored is None:
            return "new", None
        content = {k: v for k, v in event.items() if k not in ASSIGNED_FIELDS}
        stored_content = {k: v for k, v in stored.items() if k not in ASSIGNED_FIELDS}
        return ("duplicate" if content == stored_content else "conflict"), stored

    def _quarantine_event(self, event, stored):
        self._counter += 1
        reason = ("event_id redelivered with different content"
                  if stored["event_id"] == event["event_id"]
                  else "(source_id,generation,source_seq) redelivered with different content")
        entry = {"quarantine_id": self._counter, "reason": reason,
                 "conflicts_with_event_id": stored["event_id"], "event": dict(event)}
        flag = {"flag_id": self._counter, "reason": reason,
                "event_id": event["event_id"]}
        self._quarantine.append(entry)
        self._flags.append(flag)
        return {"status": "quarantined", "reason": reason,
                "flag_id": flag["flag_id"], "quarantine_id": entry["quarantine_id"]}

    # -------------------------------------------------------- projection
    def _projection_time(self, event):
        """Effective time only — never fabricated from arrival/recorded time."""
        return event["effective_at_ms"]

    def _fold(self, events):
        """Deterministic per-resource projection from the event list.

        Fold order is (effective time, confirmed-before-uncertain, append
        order): at the same effective time a confirmed stop closes the open
        segment BEFORE lease.expired can cut it, so a trusted stop arriving
        exactly at the recovery instant R confirms usage through R with no
        uncertain gap (usage-ledger #1: at the same instant the old vector
        terminates first; confirmed evidence beats uncertainty). A
        late-arriving trusted stop inside a Lost gap likewise folds before
        the lease expiry and corrects the projection (both versions kept via
        _apply's archive). Segments are half-open [start, end); zero-length
        segments are dropped (zero usage).
        """
        def fold_key(pair):
            idx, ev = pair
            uncertain = 1 if ev["type"] == "lease.expired" else 0
            return (self._projection_time(ev), uncertain, idx)
        ordered = sorted(enumerate(events), key=fold_key)
        segments, open_seg, last_heartbeat = [], None, None
        for _, ev in ordered:
            etype = ev["type"]
            now = self._projection_time(ev)
            quantity = dict(ev["payload"].get("quantity") or {})
            if etype in _SWITCH:
                if open_seg is not None and open_seg["start_ms"] < now:
                    segments.append({**open_seg, "end_ms": now, "certainty": "confirmed"})
                open_seg = {"start_ms": now, "quantity": quantity}
            elif etype in _CLOSE:
                if open_seg is not None and open_seg["start_ms"] < now:
                    segments.append({**open_seg, "end_ms": now, "certainty": "confirmed"})
                open_seg = None
            elif etype == "heartbeat.confirmed":
                last_heartbeat = now
            elif etype == "lease.expired":
                lost_from = ev["payload"].get("last_observed_at_ms", last_heartbeat)
                if open_seg is not None and lost_from is not None:
                    cut = max(lost_from, open_seg["start_ms"])
                    if open_seg["start_ms"] < cut:
                        segments.append({**open_seg, "end_ms": cut, "certainty": "confirmed"})
                    if cut < now:  # uncertain gap H->R: not Active, not zero, not billed
                        segments.append({"start_ms": cut, "end_ms": now,
                                         "quantity": open_seg["quantity"],
                                         "certainty": "uncertain"})
                open_seg = None
        return {"segments": segments, "open": open_seg}

    def _apply(self, rkey):
        """Recompute after append; archive segments superseded by later evidence."""
        fresh = self._fold(self._by_resource[rkey])
        previous = self._current.get(rkey)
        if previous is not None:
            for seg in previous["segments"]:
                if seg not in fresh["segments"]:
                    self._archive.setdefault(rkey, []).append(
                        {**seg, "superseded_by_event_id": self._by_resource[rkey][-1]["event_id"]})
        self._current[rkey] = fresh

    def projection(self, resource=None):
        """Fresh projection (no side effects): {rkey: {"segments", "open"}}."""
        if resource is not None:
            return {resource: self._fold(self._by_resource.get(resource, []))}
        return {rkey: self._fold(events) for rkey, events in self._by_resource.items()}

    def superseded(self, resource=None):
        if resource is not None:
            return list(self._archive.get(resource, []))
        return {rkey: list(segs) for rkey, segs in self._archive.items()}

    # ------------------------------------------------------------ totals
    def summarize(self):
        """Per-resource confirmed totals in integer quantity*ms only.

        No rates, no currency, no float: without approved rate cards there is
        no dollar figure to show (issue #77). Uncertain gaps are reported as
        millisecond ranges plus retained capacity, never integrated into
        confirmed totals.
        """
        totals = {}
        for rkey, projection in self.projection().items():
            if not projection["segments"] and projection["open"] is None:
                continue  # audit-only resources (operation.*, correction.*) show no usage
            confirmed = {}
            for seg in projection["segments"]:
                if seg["certainty"] != "confirmed":
                    continue
                for meter, quantity in seg["quantity"].items():
                    confirmed[meter] = confirmed.get(meter, 0) + \
                        quantity * (seg["end_ms"] - seg["start_ms"])
            uncertain = [seg for seg in projection["segments"]
                         if seg["certainty"] == "uncertain"]
            resource_type, resource_id = rkey.split("/", 1)
            totals[rkey] = {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "confirmed_quantity_ms": confirmed,
                "uncertain_ms": sum(seg["end_ms"] - seg["start_ms"] for seg in uncertain),
                "uncertain_capacity_ranges": [
                    {"start_ms": seg["start_ms"], "end_ms": seg["end_ms"],
                     "quantity": seg["quantity"]} for seg in uncertain],
                "open_capacity": projection["open"],
            }
        return totals

    # ------------------------------------------------------------ outbox
    def _outbox_for(self, event_id):
        return self._outbox_by_event.get(event_id)

    def commit_with_outbox(self, event, outbox_payload):
        """Append the state confirmation and its outbox entry under one
        atomic sequence id (in-memory stand-in for the #76 same-transaction
        write). Redelivery returns the original receipt, never a second entry.
        """
        receipt = self.append(event)
        if receipt["status"] == "quarantined":
            return receipt
        entry = self._outbox_for(receipt["event_id"])
        if entry is None:
            self._commit_seq += 1
            entry = {"outbox_id": self._commit_seq, "commit_seq": self._commit_seq,
                     "event_id": receipt["event_id"], "payload": outbox_payload,
                     "delivered": False}
            self._outbox.append(entry)
            self._outbox_by_event[receipt["event_id"]] = entry
        return {"commit_seq": entry["commit_seq"], "outbox_id": entry["outbox_id"],
                "event": receipt, "duplicate": receipt["status"] == "duplicate"}

    def pending_outbox(self):
        return [dict(entry) for entry in self._outbox if not entry["delivered"]]

    def mark_outbox_delivered(self, outbox_id):
        for entry in self._outbox:
            if entry["outbox_id"] == outbox_id:
                entry["delivered"] = True
                return
        raise ValueError(f"unknown outbox id {outbox_id!r}")

    # ------------------------------------------------------ introspection
    def events(self, resource=None):
        if resource is not None:
            return [dict(ev) for ev in self._by_resource.get(resource, [])]
        return [dict(ev) for ev in self._events]

    def quarantine(self):
        return copy.deepcopy(self._quarantine)

    def flags(self):
        return copy.deepcopy(self._flags)

    # ------------------------------------------------------ persistence
    def to_dict(self):
        return {
            "schema_version": SCHEMA_VERSION,
            "events": copy.deepcopy(self._events),
            "quarantine": copy.deepcopy(self._quarantine),
            "flags": copy.deepcopy(self._flags),
            "outbox": copy.deepcopy(self._outbox),
            "commit_seq": self._commit_seq,
            "counter": self._counter,
            "clock": self._clock,
        }

    def to_json(self):
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data):
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported persisted schema_version")
        ledger = cls()
        # ponytail: replay through append() — O(n^2) reproject, fine for the slice;
        # the real #76 backend persists rows and projects incrementally instead
        for event in data["events"]:
            receipt = ledger.append(event)
            if receipt["status"] != "appended":
                raise ValueError("persisted events must replay as appended")
        ledger._quarantine = copy.deepcopy(data["quarantine"])
        ledger._flags = copy.deepcopy(data["flags"])
        ledger._outbox = copy.deepcopy(data["outbox"])
        ledger._outbox_by_event = {e["event_id"]: e for e in ledger._outbox}
        ledger._commit_seq = data["commit_seq"]
        ledger._counter = data["counter"]
        ledger._clock = data["clock"]
        return ledger

    @classmethod
    def from_json(cls, text):
        return cls.from_dict(json.loads(text))
