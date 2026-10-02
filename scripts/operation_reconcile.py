#!/usr/bin/env python3
"""Persisted-first operation log, fencing and reconciler for #17 (stdlib only).

Control-plane half of the #72 recovery contract as executable library
semantics (no HTTP, no Runner): the intention row is persisted BEFORE runtime
execution (requested -> dispatched -> confirmed/failed/timeout), each sandbox
keeps a single serialized operation sequence carrying a fencing generation,
and replay after a crash at ANY pipeline point converges to the same state as
a no-crash run — no double-build, no second outcome, no lost event. Late
old-generation writes and events are preserved as history only and never
mutate current state.

Reuses scripts/resource_events.py EventLedger for the durable trail
(operation.requested/succeeded/failed, lease.expired) and the state+outbox
commit; no second schema is defined here. Real control-plane additions
(Postgres transactions, outbox worker, real Runner calls) are blocked on #11.

Tests: scripts/test_operation_reconcile.py.
Spec: docs/contracts/operation-reconcile.md.
"""
import copy
import json
import threading

from resource_events import EventLedger

SCHEMA_VERSION = 1

OP_TYPES = ("suspend", "resume", "destroy")
OP_STATES = ("requested", "dispatched", "confirmed", "failed", "timeout")
NON_TERMINAL = ("requested", "dispatched", "timeout")
CRASH_POINTS = ("after_intention", "after_dispatch",
                "after_runtime_before_confirm", "after_confirm_before_event")

TARGET_STATE = {"suspend": "Suspend", "resume": "Active", "destroy": "Destroyed"}
INTERMEDIATE_STATE = {"suspend": "Suspending", "resume": "Resuming",
                      "destroy": "Destroying"}
SUSPEND_FROM = frozenset({"Active", "Idle"})
RESUME_PLAIN_FROM = frozenset({"Suspend"})
DESTROY_FROM = frozenset({"Creating", "Active", "Idle", "Suspend"})
RECONCILE_GATED = frozenset({"Error", "Lost"})


class OperationConflict(Exception):
    """409 opposite_operation: per-sandbox serialization refusal."""
    http_status = 409
    code = "opposite_operation"


class VersionConflict(Exception):
    """409 version_conflict: expected_version mismatch; re-read the sandbox."""
    http_status = 409
    code = "version_conflict"


class IdempotencyConflict(Exception):
    """409 body_conflict: same key replayed with a different body."""
    http_status = 409
    code = "body_conflict"


class RuntimeDouble:
    """Idempotent in-memory stand-in for the #11 runner.

    The persisted intention (operation_id) is the dedupe key: each intention
    executes at most once and redelivery returns the identical outcome — the
    property that makes replay-after-crash safe (no double-build).
    """

    def __init__(self):
        self._outcomes = {}
        self.calls = 0
        self.effects = 0   # actual side-effect applications (dedupe visible)

    def execute(self, op):
        self.calls += 1
        oid = op["operation_id"]
        if oid not in self._outcomes:
            self.effects += 1
            self._outcomes[oid] = self._apply(op)
        return copy.deepcopy(self._outcomes[oid])

    def _apply(self, op):
        # 202/acceptance never moved observed state; this is the Runner effect
        return {"succeeded": True,
                "observed_state": TARGET_STATE[op["op_type"]]}

    def executions(self):
        return len(self._outcomes)


class OperationLog:
    """Persisted-first operations with per-sandbox fencing generations.

    Every mutating step (intention, dispatch, outcome, event emission) is
    immediately reflected in to_dict()-able state, so a snapshot taken between
    any two steps is a valid crash image. The named _-prefixed helpers are
    deliberate mutation points for the guard tests, not extension points.
    """

    def __init__(self, ledger=None):
        self._ledger = ledger if ledger is not None else EventLedger()
        self._sandboxes = {}
        self._ops = {}
        self._by_sandbox = {}      # sandbox_id -> [operation_id] in order
        self._idempotency = {}     # key -> {"fingerprint", "operation_id"}
        self._flags = []
        self._seq = 0
        self._source_seq = 0
        self._clock = 0
        # ponytail: one global lock; per-sandbox locks if throughput matters
        self._lock = threading.RLock()

    # ------------------------------------------------------------ sandbox
    def create_sandbox(self, sandbox_id=None, desired_state="Active",
                       observed_state="Active", generation=1):
        with self._lock:
            self._seq += 1
            sid = sandbox_id or f"sbx_{self._seq:06d}"
            if sid in self._sandboxes:
                raise ValueError(f"sandbox {sid} already exists")
            sb = {
                "sandbox_id": sid,
                "workspace_id": "ws_default",
                "desired_state": desired_state,
                "observed_state": observed_state,
                "generation": generation,
                "version": 1,
                "reserved": True,
                "uncertain": False,
                "reconciled": False,
                "pending_operation": None,
                "last_confirmed_state": None,
                "error": None,
                "history": [],   # late old-generation evidence, never current
                "created_at": self._tick(),
            }
            self._sandboxes[sid] = sb
            return self._sandbox_view(sb)

    def mark_lost(self, sandbox_id, last_observed_at_ms=None):
        """Lease expiry: observed -> Lost, outcome unknown.

        The reservation is KEPT (lost_keeps_reservation): no capacity claim
        either way without stop evidence; usage beyond the last trusted
        heartbeat is uncertain — never billed as confirmed, never zero.
        """
        with self._lock:
            sb = self._sandbox(sandbox_id)
            if sb["observed_state"] == "Destroyed":
                raise OperationConflict("Destroyed is terminal")
            sb["observed_state"] = "Lost"
            sb["uncertain"] = True
            sb["version"] += 1
            self._emit_raw(sandbox_id, sb["generation"], "lease.expired",
                           {"last_observed_at_ms": last_observed_at_ms})
            return self._sandbox_view(sb)

    def mark_reconciled(self, sandbox_id):
        """Fencing/reconciliation verdict: opens Error/Lost for changes and
        resolves any outstanding operation — a timeout never implied
        not-executed; THIS is the verdict (mirrors the #76 double)."""
        with self._lock:
            sb = self._sandbox(sandbox_id)
            sb["reconciled"] = True
            if sb["pending_operation"]:
                op = self._ops[sb["pending_operation"]]
                if op["state"] in NON_TERMINAL:
                    op["state"] = "failed"
                    op["error"] = {"code": "lost_reconciled",
                                   "message": "lease expired; outcome resolved "
                                              "by reconciliation"}
                    op["updated_at"] = self._tick()
                    self.emit_operation_event(op["operation_id"])
                sb["pending_operation"] = None
            sb["version"] += 1
            return self._sandbox_view(sb)

    def mark_uncertain(self, sandbox_id, reason):
        """Flag uncertain usage without touching reservation or state."""
        with self._lock:
            sb = self._sandbox(sandbox_id)
            sb["uncertain"] = True
            self._flags.append({"flag_id": len(self._flags) + 1,
                                "reason": reason, "sandbox_id": sandbox_id,
                                "release_capacity": False})
            return self._sandbox_view(sb)

    def flag_unknown_instance(self, action):
        """Isolate an observed instance with no desired record for HUMAN
        decision. Never creates a sandbox record: no auto-adoption of
        arbitrary tenants' resources."""
        with self._lock:
            self._flags.append({
                "flag_id": len(self._flags) + 1,
                "reason": "unknown_instance",
                "instance_id": action.get("instance_id"),
                "observed_state": action.get("observed_state"),
                "claimed_sandbox_id": action.get("sandbox_id"),
                "resolution": "human_decision",
                "auto_adopt": False,
            })
            return {"flagged": True, "auto_adopt": False,
                    "flag_id": len(self._flags)}

    # ---------------------------------------------------------- operation
    def request(self, sandbox_id, op_type, idempotency_key=None,
                expected_version=None, reason="user_request"):
        """Persist the intention FIRST; runtime execution comes later.

        The returned operation row is the dedupe key for every later retry or
        replay: same key/same intention -> the same row; opposite operation
        while one is pending -> 409, never interleaved.
        """
        if op_type not in OP_TYPES:
            raise ValueError(f"op_type must be one of {OP_TYPES}")
        with self._lock:
            sb = self._sandbox(sandbox_id)
            fingerprint = json.dumps({"sandbox_id": sandbox_id,
                                      "op_type": op_type},
                                     sort_keys=True, separators=(",", ":"))
            if idempotency_key is not None:
                entry = self._idempotent_lookup(idempotency_key)
                if entry is not None:
                    if entry["fingerprint"] != fingerprint:
                        raise IdempotencyConflict(
                            "Idempotency-Key replayed with a different body")
                    return {"status": "replayed", "operation": self._op_view(
                        self._ops[entry["operation_id"]])}
            if sb["observed_state"] == "Destroyed" and op_type == "destroy":
                prior = self._last_destroy_op(sb)  # repeat destroy: same op
                if prior is not None:
                    return {"status": "replayed",
                            "operation": self._op_view(prior)}
            if expected_version is None or expected_version != sb["version"]:
                raise VersionConflict(
                    "expected_version mismatch; re-read the sandbox")
            pending = self._pending(sb)
            if pending is not None:
                if pending["op_type"] == op_type:
                    return {"status": "pending_replay",
                            "operation": self._op_view(pending)}
                if self._conflicts(pending, op_type):
                    raise OperationConflict(
                        f"{pending['op_type']} is already pending on "
                        f"{sandbox_id}; opposite operations never interleave")
            self._accept(sb, op_type)
            self._seq += 1
            op = {
                "operation_id": f"op_{self._seq:06d}",
                "sandbox_id": sandbox_id,
                "op_type": op_type,
                "target": {"state": TARGET_STATE[op_type]},
                "state": "requested",
                "expected_version": expected_version,
                "generation": sb["generation"],
                "result": None,
                "error": None,
                "events": [],
                "history": [],
                "created_at": self._tick(),
                "updated_at": self._clock,
            }
            self._ops[op["operation_id"]] = op
            self._by_sandbox.setdefault(sandbox_id, []).append(
                op["operation_id"])
            sb["pending_operation"] = op["operation_id"]
            sb["version"] += 1
            self._emit(op, "operation.requested",
                       {"reason": reason, "target": dict(op["target"]),
                        "expected_version": expected_version,
                        "generation": op["generation"]})
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = {
                    "fingerprint": fingerprint,
                    "operation_id": op["operation_id"]}
            return {"status": "accepted", "operation": self._op_view(op)}

    def dispatch(self, op_id):
        """Hand the persisted intention to the runtime (idempotent)."""
        with self._lock:
            op = self._op(op_id)
            sb = self._sandboxes[op["sandbox_id"]]
            if not self._generation_ok(op["generation"], sb["generation"]):
                return self._stale_write(op, "dispatch")
            if op["state"] in ("requested", "timeout"):
                op["state"] = "dispatched"
                op["updated_at"] = self._tick()
            return {"status": op["state"], "operation": self._op_view(op)}

    def mark_timeout(self, op_id):
        """Request timeout: outcome UNKNOWN — pollable and re-dispatchable,
        NEVER failed. Failure requires a confirmed verdict."""
        with self._lock:
            op = self._op(op_id)
            sb = self._sandboxes[op["sandbox_id"]]
            if not self._generation_ok(op["generation"], sb["generation"]):
                return self._stale_write(op, "mark_timeout")
            if op["state"] in ("requested", "dispatched"):
                op["state"] = "timeout"
                op["updated_at"] = self._tick()
            return {"status": op["state"], "operation": self._op_view(op)}

    def runtime_outcome(self, op_id, outcome):
        """Persist the runtime verdict: exactly one terminal outcome per
        intention. Old-generation or late writes are preserved as history
        only and never mutate current state."""
        with self._lock:
            op = self._op(op_id)
            sb = self._sandboxes[op["sandbox_id"]]
            if not self._generation_ok(op["generation"], sb["generation"]):
                return self._stale_write(op, "runtime_outcome", outcome)
            if op["state"] in ("confirmed", "failed"):
                op["history"].append({"write": "runtime_outcome",
                                      "status": "late_terminal",
                                      "outcome": copy.deepcopy(outcome)})
                return {"status": "terminal_unchanged",
                        "operation": self._op_view(op)}
            if op["state"] not in NON_TERMINAL:
                return {"status": op["state"], "operation": self._op_view(op)}
            if outcome.get("succeeded"):
                op["state"] = "confirmed"
                op["result"] = {"observed_state": outcome["observed_state"],
                                "generation": op["generation"]}
                sb["observed_state"] = outcome["observed_state"]
                sb["error"] = None
                sb["uncertain"] = False
                sb["last_confirmed_state"] = outcome["observed_state"]
                if outcome["observed_state"] == "Destroyed":
                    sb["reserved"] = False  # released only at confirmed removal
            else:
                error = outcome.get("error") or {}
                op["state"] = "failed"
                op["error"] = {"code": error.get("code", "operation_failed"),
                               "message": error.get("message", "")}
                sb["last_confirmed_state"] = sb["observed_state"]
                sb["observed_state"] = "Error"
                sb["error"] = {"code": op["error"]["code"], "resources": []}
            sb["pending_operation"] = None
            sb["version"] += 1
            op["updated_at"] = self._tick()
            return {"status": op["state"], "operation": self._op_view(op)}

    def emit_operation_event(self, op_id):
        """Append the terminal operation event (and its outbox entry) to the
        shared EventLedger. Deterministic event ids make re-emission after a
        crash a dedupe, never a second event."""
        with self._lock:
            op = self._op(op_id)
            if op["state"] == "confirmed":
                etype = "operation.succeeded"
                payload = {"result": copy.deepcopy(op["result"])}
            elif op["state"] == "failed":
                etype = "operation.failed"
                payload = {"error": copy.deepcopy(op["error"])}
            else:
                return {"status": op["state"]}
            if self._event_emitted(op, etype):
                return {"status": "emitted"}
            self._emit(op, etype, payload, outbox=True)
            return {"status": "emitted"}

    def replay(self, runtime):
        """Converge after a crash/restart to the same state as no-crash.

        Every non-terminal operation is re-dispatched and settled through the
        idempotent runtime (intention row = dedupe key, so no double-build);
        terminal operations missing their ledger event get it emitted. Safe to
        call repeatedly on an already-converged log.
        """
        with self._lock:
            converged = []
            for op_id, op in list(self._ops.items()):
                if op["state"] in NON_TERMINAL:
                    if op["state"] != "dispatched":
                        self.dispatch(op_id)
                    outcome = runtime.execute(self._op_view(op))
                    self.runtime_outcome(op_id, outcome)
                    converged.append(op_id)
                if op["state"] in ("confirmed", "failed"):
                    self.emit_operation_event(op_id)
            return {"converged": converged}

    def apply_event(self, sandbox_id, generation, observed_state=None,
                    evidence=None):
        """Runner evidence submission with generation fencing: late
        old-generation evidence is appended to history ONLY and never
        mutates current state."""
        with self._lock:
            sb = self._sandbox(sandbox_id)
            fenced = not self._generation_ok(generation, sb["generation"])
            entry = {"kind": "late_event" if fenced else "event",
                     "generation": generation,
                     "observed_state": observed_state,
                     "evidence": copy.deepcopy(evidence or {})}
            if fenced:
                sb["history"].append({**entry, "status": "history_only"})
                return {"status": "history_only"}
            if observed_state is not None:
                sb["observed_state"] = observed_state
                if observed_state in ("Suspend", "Destroyed"):
                    sb["uncertain"] = False
                sb["version"] += 1
            return {"status": "applied"}

    # ----------------------------------------------------- acceptance rules
    def _accept(self, sb, op_type):
        observed = sb["observed_state"]
        if op_type == "suspend":
            if observed not in SUSPEND_FROM:
                raise OperationConflict(
                    f"suspend is not accepted while {observed}")
            sb["observed_state"] = INTERMEDIATE_STATE["suspend"]
            sb["desired_state"] = TARGET_STATE["suspend"]
        elif op_type == "resume":
            if not (observed in RESUME_PLAIN_FROM
                    or (observed in RECONCILE_GATED and sb["reconciled"])):
                raise OperationConflict(
                    f"resume is not accepted while {observed}"
                    + ("" if observed == "Suspend"
                       else " (reconciliation/fencing required)"))
            sb["generation"] += 1  # new generation + fencing token; old fenced
            sb["observed_state"] = INTERMEDIATE_STATE["resume"]
            sb["desired_state"] = TARGET_STATE["resume"]
            sb["reconciled"] = False
        else:
            if observed in RECONCILE_GATED and not sb["reconciled"]:
                raise OperationConflict(
                    "reconciliation/fencing must precede destroy")
            if observed not in DESTROY_FROM and observed not in RECONCILE_GATED:
                raise OperationConflict(
                    f"destroy is not accepted while {observed}")
            sb["observed_state"] = INTERMEDIATE_STATE["destroy"]
            sb["desired_state"] = TARGET_STATE["destroy"]

    def _pending(self, sb):
        if not sb["pending_operation"]:
            return None
        return self._ops.get(sb["pending_operation"])

    def _conflicts(self, pending, op_type):
        return pending["op_type"] != op_type

    def _generation_ok(self, generation, current_generation):
        return generation >= current_generation

    def _event_emitted(self, op, etype):
        return etype in op["events"]

    def _idempotent_lookup(self, key):
        return self._idempotency.get(key)

    def _last_destroy_op(self, sb):
        for op_id in reversed(self._by_sandbox.get(sb["sandbox_id"], [])):
            if self._ops[op_id]["op_type"] == "destroy":
                return self._ops[op_id]
        return None

    def _stale_write(self, op, write, extra=None):
        """Old-generation write: rejected for CURRENT state, preserved as
        history (the real backend rejects the row write; the audit trail
        keeps the attempt)."""
        op["history"].append({"write": write, "status": "stale_generation",
                              "generation": op["generation"],
                              "detail": copy.deepcopy(extra)})
        return {"status": "stale_generation", "operation": self._op_view(op)}

    # ------------------------------------------------------------- events
    def _tick(self):
        self._clock += 1
        return self._clock

    def _emit(self, op, etype, payload, outbox=False):
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"{op['operation_id']}:{etype}",
            "tenant_id": "t_default",
            "workspace_id": op.get("workspace_id", "ws_default"),
            "resource_type": "sandbox",
            "resource_id": op["sandbox_id"],
            "source_id": "control-plane",
            "source_seq": self._next_source_seq(),
            "effective_at_ms": self._tick(),
            "type": etype,
            "reason": op["op_type"],
            "payload": payload,
            "generation": op["generation"],
        }
        self._append_event(event, outbox, op["operation_id"])
        op["events"].append(etype)

    def _emit_raw(self, sandbox_id, generation, etype, payload):
        self._seq += 1
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"{sandbox_id}:{etype}:{self._seq}",
            "tenant_id": "t_default",
            "workspace_id": "ws_default",
            "resource_type": "sandbox",
            "resource_id": sandbox_id,
            "source_id": "control-plane",
            "source_seq": self._next_source_seq(),
            "effective_at_ms": self._tick(),
            "type": etype,
            "reason": "lease_expiry",
            "payload": payload,
            "generation": generation,
        }
        self._append_event(event, False, None)

    def _append_event(self, event, outbox, operation_id):
        if outbox:
            self._ledger.commit_with_outbox(
                event, {"topic": event["type"],
                        "operation_id": operation_id,
                        "sandbox_id": event["resource_id"]})
        else:
            self._ledger.append(event)

    def _next_source_seq(self):
        self._source_seq += 1
        return self._source_seq

    # ------------------------------------------------------ introspection
    def _sandbox(self, sid):
        sb = self._sandboxes.get(sid)
        if sb is None:
            raise ValueError(f"unknown sandbox {sid!r}")
        return sb

    def _op(self, op_id):
        op = self._ops.get(op_id)
        if op is None:
            raise ValueError(f"unknown operation {op_id!r}")
        return op

    def sandbox(self, sandbox_id):
        with self._lock:
            return self._sandbox_view(self._sandbox(sandbox_id))

    def sandboxes(self):
        with self._lock:
            return {sid: self._sandbox_view(sb)
                    for sid, sb in self._sandboxes.items()}

    def operation(self, op_id):
        with self._lock:
            return self._op_view(self._op(op_id))

    def operations(self, sandbox_id=None):
        """Per-sandbox single sequence, in acceptance order."""
        with self._lock:
            ids = (self._by_sandbox.get(sandbox_id, [])
                   if sandbox_id is not None else list(self._ops))
            return [self._op_view(self._ops[i]) for i in ids]

    def poll(self, op_id):
        return self.operation(op_id)

    def flags(self):
        with self._lock:
            return copy.deepcopy(self._flags)

    def ledger(self):
        return self._ledger

    def _op_view(self, op):
        return {
            "operation_id": op["operation_id"],
            "sandbox_id": op["sandbox_id"],
            "op_type": op["op_type"],
            "target": copy.deepcopy(op["target"]),
            "state": op["state"],
            "expected_version": op["expected_version"],
            "generation": op["generation"],
            "result": copy.deepcopy(op["result"]),
            "error": copy.deepcopy(op["error"]),
            "events": list(op["events"]),
            "history": copy.deepcopy(op["history"]),
            "retryable": op["state"] in ("failed", "timeout"),
            "timeout_is_not_failed": op["state"] == "timeout",
            "created_at": op["created_at"],
            "updated_at": op["updated_at"],
        }

    def _sandbox_view(self, sb):
        return {
            "sandbox_id": sb["sandbox_id"],
            "workspace_id": sb["workspace_id"],
            "desired_state": sb["desired_state"],
            "observed_state": sb["observed_state"],
            "generation": sb["generation"],
            "version": sb["version"],
            "reserved": sb["reserved"],
            "uncertain": sb["uncertain"],
            "reconciled": sb["reconciled"],
            "pending_operation": sb["pending_operation"],
            "last_confirmed_state": sb["last_confirmed_state"],
            "error": copy.deepcopy(sb["error"]),
            "history": copy.deepcopy(sb["history"]),
            "created_at": sb["created_at"],
        }

    # -------------------------------------------------------- persistence
    def to_dict(self):
        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "seq": self._seq,
                "source_seq": self._source_seq,
                "clock": self._clock,
                "sandboxes": copy.deepcopy(self._sandboxes),
                "ops": copy.deepcopy(self._ops),
                "idempotency": {k: dict(v) for k, v in
                                self._idempotency.items()},
                "flags": copy.deepcopy(self._flags),
                "ledger": self._ledger.to_dict(),
            }

    def to_json(self):
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data):
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported persisted schema_version")
        log = cls(ledger=EventLedger.from_dict(data["ledger"]))
        log._seq = data["seq"]
        log._source_seq = data["source_seq"]
        log._clock = data["clock"]
        log._sandboxes = copy.deepcopy(data["sandboxes"])
        log._ops = copy.deepcopy(data["ops"])
        log._idempotency = {k: dict(v) for k, v in
                            data["idempotency"].items()}
        log._flags = copy.deepcopy(data["flags"])
        log._by_sandbox = {}
        for op_id, op in log._ops.items():  # restore per-sandbox order
            log._by_sandbox.setdefault(op["sandbox_id"], []).append(op_id)
        return log

    @classmethod
    def from_json(cls, text):
        return cls.from_dict(json.loads(text))


class Reconciler:
    """diff(desired, observed) -> actions; apply() drives them through the
    OperationLog. Pure diff: deciding is separate from doing, so the action
    list is auditable before anything mutates."""

    def __init__(self, log):
        self.log = log

    def diff(self, desired, observed):
        """desired: {sandbox_id: sandbox view} (e.g. log.sandboxes()).
        observed: {instance_id: {"sandbox_id", "state", "generation",
        "stop_evidence": bool}} — runner inventory evidence."""
        actions = []
        by_sandbox = {}
        unknown = []
        for iid, inst in observed.items():
            sid = inst.get("sandbox_id")
            rec = desired.get(sid)
            # Only the CURRENT-generation instance is evidence for a record;
            # stale/rogue or duplicate-generation claims route to isolation —
            # a recreated cgroup's zeroed counters are not evidence.
            if rec is not None and inst.get("generation") == rec.get("generation"):
                by_sandbox.setdefault(sid, inst)
            else:
                unknown.append((iid, inst))
        # unknown instances first: isolate for human decision, never adopt
        for iid, inst in sorted(unknown, key=lambda pair: pair[0]):
            actions.append({"action": "isolate_unknown_instance",
                            "instance_id": iid,
                            "sandbox_id": inst.get("sandbox_id"),
                            "observed_state": inst.get("state"),
                            "auto_adopt": False,
                            "requires": "human_decision"})
        for sid in sorted(desired):
            rec = desired[sid]
            inst = by_sandbox.get(sid)
            observed_state = inst["state"] if inst else rec["observed_state"]
            stop_evidence = bool(inst and inst.get("stop_evidence"))
            lost = rec["observed_state"] == "Lost" or observed_state == "Lost"
            if lost and not stop_evidence:
                # Lost with no stop evidence: keep the reservation, mark the
                # usage uncertain; no capacity claim either way, no recreate
                actions.append({"action": "keep_reservation_mark_uncertain",
                                "sandbox_id": sid,
                                "stop_evidence": False,
                                "release_capacity": False})
                continue
            if (rec["desired_state"] == "Suspend"
                    and observed_state in ("Active", "Idle")):
                actions.append({"action": "request_suspend", "sandbox_id": sid})
            elif (rec["desired_state"] == "Active"
                    and observed_state in ("Error", "Lost")
                    and (rec["reconciled"] or stop_evidence)):
                # recreation: Error/Lost -> Resuming -> new generation;
                # the bump fences every old-generation write
                actions.append({"action": "recreate_new_generation",
                                "sandbox_id": sid,
                                "path": [observed_state, "Resuming", "Active"],
                                "bump_generation": True,
                                "fence_old": True})
        return actions

    def apply(self, actions):
        outcomes = []
        for action in actions:
            kind = action["action"]
            if kind == "request_suspend":
                sb = self.log.sandbox(action["sandbox_id"])
                outcomes.append(self.log.request(
                    action["sandbox_id"], "suspend",
                    expected_version=sb["version"], reason="reconciler_desired"))
            elif kind == "recreate_new_generation":
                sb = self.log.sandbox(action["sandbox_id"])
                if not sb["reconciled"]:
                    self.log.mark_reconciled(action["sandbox_id"])
                    sb = self.log.sandbox(action["sandbox_id"])
                outcomes.append(self.log.request(
                    action["sandbox_id"], "resume",
                    expected_version=sb["version"], reason="recreation"))
            elif kind == "isolate_unknown_instance":
                outcomes.append(self.log.flag_unknown_instance(action))
            elif kind == "keep_reservation_mark_uncertain":
                outcomes.append(self.log.mark_uncertain(
                    action["sandbox_id"], reason="lost_no_stop_evidence"))
            else:
                raise ValueError(f"unknown action {kind!r}")
        return outcomes


# ------------------------------------------------------------ crash matrix
INITIAL_STATE = {"suspend": "Active", "resume": "Suspend", "destroy": "Active"}
DESIRED_STATE = {"suspend": "Suspend", "resume": "Active", "destroy": "Destroyed"}
MATRIX_SID = "sbx_matrix"


def _pipeline(log, runtime, op_type, crash_at=None):
    """Persisted-first pipeline: intention -> dispatch -> runtime effect ->
    persisted verdict -> ledger event. Returns the crash snapshot at
    crash_at, or None when the run completed."""
    sb = log.sandbox(MATRIX_SID)
    receipt = log.request(MATRIX_SID, op_type,
                          idempotency_key=f"key-{op_type}",
                          expected_version=sb["version"])
    op_id = receipt["operation"]["operation_id"]
    if crash_at == "after_intention":
        return log.to_dict()
    log.dispatch(op_id)
    if crash_at == "after_dispatch":
        return log.to_dict()
    runtime.execute(log.operation(op_id))  # real-world effect happens here
    if crash_at == "after_runtime_before_confirm":
        return log.to_dict()                # DB write lost — runtime already ran
    log.runtime_outcome(op_id, runtime.execute(log.operation(op_id)))
    if crash_at == "after_confirm_before_event":
        return log.to_dict()                # verdict persisted, event not yet
    log.emit_operation_event(op_id)
    return None


def converged_view(log, runtime):
    """Comparable summary for crash-matrix convergence: sandbox count,
    per-sandbox generation, operation outcomes, ledger event count and
    runtime execution count (double-build detector)."""
    sandboxes = log.sandboxes()
    return {
        "sandbox_count": len(sandboxes),
        "sandboxes": {sid: {"observed_state": s["observed_state"],
                            "generation": s["generation"],
                            "reserved": s["reserved"],
                            "desired_state": s["desired_state"]}
                      for sid, s in sandboxes.items()},
        "operation_states": {o["operation_id"]: o["state"]
                             for o in log.operations()},
        "event_count": len(log.ledger().events()),
        "runtime_executions": runtime.effects,
    }


def simulate_crash(op_type, point):
    """One crash-matrix cell: run the pipeline for op_type, die at `point`,
    restore the persisted snapshot into a fresh log, replay, and return
    (recovered, clean) converged views for equality comparison. The runtime
    is shared across the crash (it is the real world): its execution count
    proves replay never double-builds."""
    if op_type not in OP_TYPES:
        raise ValueError(f"op_type must be one of {OP_TYPES}")
    if point not in CRASH_POINTS:
        raise ValueError(f"point must be one of {CRASH_POINTS}")

    clean_log, clean_rt = OperationLog(), RuntimeDouble()
    clean_log.create_sandbox(MATRIX_SID, desired_state=DESIRED_STATE[op_type],
                             observed_state=INITIAL_STATE[op_type])
    _pipeline(clean_log, clean_rt, op_type)
    clean_log.replay(clean_rt)  # idempotent on a converged log

    log, rt = OperationLog(), RuntimeDouble()
    log.create_sandbox(MATRIX_SID, desired_state=DESIRED_STATE[op_type],
                       observed_state=INITIAL_STATE[op_type])
    snapshot = _pipeline(log, rt, op_type, crash_at=point)
    recovered = OperationLog.from_dict(snapshot)
    recovered.replay(rt)
    return {"recovered": converged_view(recovered, rt),
            "clean": converged_view(clean_log, clean_rt),
            "runtime": rt}


if __name__ == "__main__":
    failures = 0
    for op_type in OP_TYPES:
        for point in CRASH_POINTS:
            result = simulate_crash(op_type, point)
            ok = result["recovered"] == result["clean"]
            failures += 0 if ok else 1
            print(f"{op_type:8s} {point:32s} "
                  f"{'converged' if ok else 'DIVERGED'}")
    raise SystemExit(1 if failures else 0)
