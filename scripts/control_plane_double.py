#!/usr/bin/env python3
"""Executable test double of the control-plane API contract.

Implements docs/contracts/control-plane-api.json in memory, stdlib-only, with
no HTTP server: ControlPlaneDouble.request(method, path, headers, body) returns
(status, json-body). The state machine is NOT duplicated here — every observed
state move is looked up in docs/contracts/runner-lifecycle.json and
refuse-on-unknown applies. observed_state changes only through the confirm() /
expire_lease() test hooks (stand-ins for Runner evidence): HTTP 202 alone never
moves a sandbox into a confirmed state.

Contract tests: scripts/test_control_plane_contract.py.
"""
import copy
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_RUNNER_CONTRACT = REPO / "docs/contracts" / "runner-lifecycle.json"

ACCEPTED_TARGETS = {"Active", "Idle", "Suspend"}
SUSPEND_MODES = {"cold"}
CONFIRM_SCOPE = "workspace_and_home_volumes"
# Acceptance-driven intermediate states: entering these is triggered by
# operation acceptance per the runner contract ("requires: operation accepted");
# every other transition is applied only by confirm() (Runner evidence).
INTERMEDIATE_STATES = {"Suspending", "Resuming", "Destroying"}
# ponytail: capacity is derived from observed_state, not an accounting ledger —
# Suspend releases compute only at the confirmed stop, Destroyed at confirmed removal
COMPUTE_HELD_STATES = {"Creating", "Active", "Idle", "Suspending", "Resuming",
                       "Destroying", "Lost", "Error"}
TERMINAL_TICKET_TTL_MS = 60000


def _err(status, code, message):
    return status, {"error": {"code": code, "message": message}}


class _Refused(Exception):
    def __init__(self, payload):
        self.payload = payload


class ControlPlaneDouble:
    """In-memory control plane per docs/contracts/control-plane-api.json.

    Single tenant (M1): one fixed workspace; unknown workspace/sandbox ids
    answer 404 by construction. Overrides named *_* hooks are deliberate
    mutation points for the guard tests, not extension points.
    """

    def __init__(self, token="test-token", capacity=None,
                 runner_contract_path=DEFAULT_RUNNER_CONTRACT):
        self.token = token
        doc = json.loads(Path(runner_contract_path).read_text())
        self.initial_state = doc["initial_state"]
        # a (from, trigger) pair can carry both the success target and Error
        self._transitions = {}
        for t in doc["transitions"]:
            self._transitions.setdefault((t["from"], t["trigger"]), []).append(t["to"])
        self._error_capable = {t["from"] for t in doc["transitions"]
                               if t["to"] == "Error"}
        self.capacity = dict(capacity or {"milli_cpu": 8000,
                                          "memory_bytes": 16 << 30,
                                          "volume_bytes": 200 << 30})
        self._clock = 0
        self._seq = 0
        self.sandboxes = {}
        self.operations = {}
        self.idempotency = {}
        self._tickets = {}  # deliberately not snapshotted: restart drops attaches

    # ------------------------------------------------------------------ API
    def request(self, method, path, headers=None, body=None):
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        if not self._authorized(headers):
            return _err(401, "unauthorized", "missing or invalid bearer token")
        parts = [p for p in str(path).split("?")[0].split("/") if p]
        try:
            return self._route(method.upper(), parts, headers, dict(body or {}))
        except _Refused as refused:
            return refused.payload

    def _route(self, method, parts, headers, body):
        if parts[:2] == ["v1", "sandboxes"]:
            if len(parts) == 2 and method == "POST":
                return self._create(headers, body)
            if len(parts) == 2 and method == "GET":
                return self._list()
            if len(parts) >= 3:
                sb = self._sandbox(parts[2])
                if len(parts) == 3 and method == "GET":
                    return 200, self._sandbox_view(sb)
                if len(parts) == 3 and method == "DELETE":
                    return self._destroy(sb, headers, body)
                if parts[3:] == ["state"] and method == "POST":
                    return self._change_state(sb, headers, body)
                if parts[3:] == ["terminal-ticket"] and method == "POST":
                    return self._issue_ticket(sb)
        if parts[:2] == ["v1", "operations"] and len(parts) == 3 and method == "GET":
            return 200, self._op_view(self._operation(parts[2]))
        return _err(404, "not_found", "unknown endpoint")

    # ------------------------------------------------------------ endpoints
    def _create(self, headers, body):
        entry = self._idempotency_check("POST", "/v1/sandboxes", headers, body)
        if entry is not None:
            return self._idempotent_replay(entry)
        if body.get("agent") != "claude":
            raise _Refused(_err(422, "invalid", "only agent=claude is accepted"))
        resources = self._validated_resources(body)
        repo_url = body.get("repo_url")
        if repo_url is not None and (
                not isinstance(repo_url, str) or not repo_url.startswith("https://")):
            raise _Refused(_err(422, "invalid", "repo_url must be an https URL"))
        deadline = body.get("runtime_deadline_at")
        if deadline is not None and (not isinstance(deadline, int) or deadline <= 0):
            raise _Refused(_err(422, "invalid", "runtime_deadline_at must be a positive UTC ms epoch or null"))
        left = self._capacity_left()
        if (resources["milli_cpu"] > left["milli_cpu"]
                or resources["memory_bytes"] > left["memory_bytes"]
                or resources["volume_bytes"] > left["volume_bytes"]):
            # ponytail: API edge maps admission rejection to 429 per the #76
            # slice; runner-lifecycle.json records 409 runner-side — reconcile at #11
            raise _Refused(_err(429, "capacity_exceeded",
                                "admission rejected: insufficient host capacity"))
        sid = self._next_id("sbx")
        sb = {
            "sandbox_id": sid,
            "workspace_id": "ws_default",
            "desired_state": "Active",
            "observed_state": self.initial_state,
            "generation": 1,
            "version": 1,
            "resources": resources,
            "repo_url": repo_url,
            "runtime_deadline_at": deadline,
            "volumes": {"workspace": self._next_id("vol"),
                        "home": self._next_id("vol")},
            "session": None,
            "pending_operation": None,
            "last_confirmed_at": None,
            "error": None,
            "last_confirmed_state": None,
            "reconciled": False,
            "destroy_operation": None,
            "has_credential": False,
            "created_at": self._tick(),
        }
        self._apply_create_observation(sb)
        op = self._new_operation(sb, "create", None, None)
        self.sandboxes[sid] = sb
        self._remember(headers.get("idempotency-key"), "POST", "/v1/sandboxes",
                       body, 202, op)
        return 202, {"sandbox_id": sid, "operation": self._op_view(op)}

    def _list(self):
        return 200, {"sandboxes": [
            {"sandbox_id": sb["sandbox_id"], "observed_state": sb["observed_state"],
             "desired_state": sb["desired_state"], "generation": sb["generation"]}
            for sb in self.sandboxes.values() if sb["observed_state"] != "Destroyed"]}

    def _change_state(self, sb, headers, body):
        entry = self._idempotency_check(
            "POST", f"/v1/sandboxes/{sb['sandbox_id']}/state", headers, body)
        if entry is not None:
            return self._idempotent_replay(entry)
        target = body.get("state")
        if target not in ACCEPTED_TARGETS:
            raise _Refused(_err(422, "invalid",
                                "state must be one of Active, Idle, Suspend"))
        mode = body.get("suspend_mode", "cold")
        if mode not in SUSPEND_MODES:
            raise _Refused(_err(422, "unsupported_suspend_mode",
                                "first-version API accepts suspend_mode=cold only"))
        expected = body.get("expected_version")
        if not isinstance(expected, int):
            raise _Refused(_err(422, "invalid", "expected_version integer required"))
        if not self._version_ok(sb, expected):
            raise _Refused(_err(409, "version_conflict",
                                "expected_version mismatch; re-read the sandbox"))
        if (sb["desired_state"] == target and sb["observed_state"] == target
                and sb["pending_operation"] is None):
            return 200, {"sandbox": self._sandbox_view(sb)}
        pending = (self.operations.get(sb["pending_operation"])
                   if sb["pending_operation"] else None)
        if pending is not None:
            if pending["target"].get("state") == target:
                self._remember(headers.get("idempotency-key"), "POST",
                               f"/v1/sandboxes/{sb['sandbox_id']}/state",
                               body, 202, pending)
                return 202, {"operation": self._op_view(pending)}
            raise _Refused(_err(409, "opposite_operation",
                                "another change operation is already pending"))
        refusal = self._state_refusal(sb, target)
        if refusal is not None:
            raise _Refused(_err(*refusal))
        trigger = self._trigger_for(sb["observed_state"], target)
        if (sb["observed_state"], trigger) not in self._transitions:
            raise _Refused(_err(409, "opposite_operation",
                                f"operation {target} is not defined for state "
                                f"{sb['observed_state']}"))
        credential = body.get("credential")
        if trigger == "resume" and not credential:
            raise _Refused(_err(409, "credentials_required",
                                "resume needs the credential re-sent; no operation created"))
        if trigger == "resume":
            left = self._capacity_left()
            if (sb["resources"]["milli_cpu"] > left["milli_cpu"]
                    or sb["resources"]["memory_bytes"] > left["memory_bytes"]):
                raise _Refused(_err(429, "capacity_exceeded",
                                    "admission rejected: insufficient host capacity"))
        op = self._new_operation(sb, "set_state", trigger,
                                 {"state": target, "suspend_mode": mode})
        intermediate = self._success_target(sb["observed_state"], trigger)
        if trigger == "resume":
            sb["generation"] += 1  # cold resume allocates a new generation
        if intermediate in INTERMEDIATE_STATES:
            sb["observed_state"] = intermediate
        sb["desired_state"] = target
        sb["version"] += 1
        self._store_credential(sb, op, credential)
        self._remember(headers.get("idempotency-key"), "POST",
                       f"/v1/sandboxes/{sb['sandbox_id']}/state", body, 202, op)
        return 202, {"operation": self._op_view(op)}

    def _destroy(self, sb, headers, body):
        entry = self._idempotency_check(
            "DELETE", f"/v1/sandboxes/{sb['sandbox_id']}", headers, body)
        if entry is not None:
            return self._idempotent_replay(entry)
        expected = body.get("expected_version")
        if not isinstance(expected, int):
            raise _Refused(_err(422, "invalid", "expected_version integer required"))
        if body.get("confirm_scope") != CONFIRM_SCOPE:
            raise _Refused(_err(422, "invalid",
                                f"confirm_scope must explicitly be {CONFIRM_SCOPE}"))
        if not self._version_ok(sb, expected):
            raise _Refused(_err(409, "version_conflict",
                                "expected_version mismatch; re-read the sandbox"))
        if sb["observed_state"] == "Destroyed" and sb["destroy_operation"]:
            op = self.operations[sb["destroy_operation"]]
            self._remember(headers.get("idempotency-key"), "DELETE",
                           f"/v1/sandboxes/{sb['sandbox_id']}", body, 202, op)
            return 202, {"operation": self._op_view(op)}  # repeat destroy: same op
        if sb["observed_state"] == "Destroying" and sb["pending_operation"]:
            op = self.operations[sb["pending_operation"]]
            if op["type"] == "destroy":
                self._remember(headers.get("idempotency-key"), "DELETE",
                               f"/v1/sandboxes/{sb['sandbox_id']}", body, 202, op)
                return 202, {"operation": self._op_view(op)}
        if (sb["observed_state"], "destroy") not in self._transitions:
            raise _Refused(_err(409, "opposite_operation",
                                f"destroy is not accepted while {sb['observed_state']}"))
        if sb["observed_state"] in {"Error", "Lost"} and not sb["reconciled"]:
            raise _Refused(_err(409, "opposite_operation",
                                "reconciliation/fencing must precede destroy"))
        op = self._new_operation(sb, "destroy", "destroy",
                                 {"scope": CONFIRM_SCOPE})
        sb["observed_state"] = "Destroying"
        sb["desired_state"] = "Destroyed"
        sb["version"] += 1
        sb["destroy_operation"] = op["operation_id"]
        self._remember(headers.get("idempotency-key"), "DELETE",
                       f"/v1/sandboxes/{sb['sandbox_id']}", body, 202, op)
        return 202, {"operation": self._op_view(op)}

    def _issue_ticket(self, sb):
        if sb["observed_state"] == "Destroyed":
            raise _Refused(_err(404, "not_found", "sandbox not found"))
        ticket = self._next_id("tt")
        self._tickets[ticket] = {"sandbox_id": sb["sandbox_id"],
                                 "generation": sb["generation"]}
        return 200, {"ticket": ticket, "sandbox_id": sb["sandbox_id"],
                     "generation": sb["generation"],
                     "expires_in_ms": TERMINAL_TICKET_TTL_MS}

    # ------------------------------------------------------- Runner-evidence hooks
    def advance(self, operation_id):
        """Simulate the Runner picking the operation up: pending -> running."""
        op = self.operations[operation_id]
        if op["state"] == "pending":
            op["state"] = "running"
            op["updated_at"] = self._tick()
        return self._op_view(op)

    def confirm(self, operation_id, error=None):
        """Simulate Runner confirmation (success or known failure)."""
        op = self.operations[operation_id]
        if op["state"] in {"succeeded", "failed"}:
            return self._op_view(op)
        sb = self.sandboxes[op["sandbox_id"]]
        trigger = op["trigger"]
        if error is not None:
            if sb["observed_state"] not in self._error_capable:
                raise RuntimeError(
                    f"no {sb['observed_state']}->Error transition in contract")
            to_state = "Error"
        else:
            to_state = self._success_target(sb["observed_state"], trigger)
            if to_state is None:
                raise RuntimeError(
                    f"no transition {sb['observed_state']} --{trigger}--> in contract")
        if error is not None:
            op["state"] = "failed"
            op["error"] = {"code": error.get("code", "operation_failed"),
                           "message": error.get("message", "")}
            sb["last_confirmed_state"] = sb["observed_state"]
            sb["error"] = {"code": op["error"]["code"], "resources": []}
        else:
            op["state"] = "succeeded"
            op["result"] = {"observed_state": to_state,
                            "generation": sb["generation"]}
        sb["observed_state"] = to_state
        if to_state == "Active" and trigger == "create":
            sb["session"] = {"id": f"claude-session-{sb['sandbox_id']}",
                             "cwd": "/workspace"}
        if to_state == "Suspend":
            sb["has_credential"] = False  # runtime tmpfs cleared at confirmed stop
        if to_state == "Destroyed":
            sb["session"] = None
        if sb["pending_operation"] == operation_id:
            sb["pending_operation"] = None
        sb["last_confirmed_at"] = self._tick()
        sb["version"] += 1
        sb["reconciled"] = False
        op["updated_at"] = self._tick()
        return self._op_view(op)

    def expire_lease(self, sandbox_id):
        """Simulate lease expiry: observed -> Lost, outcome unknown."""
        sb = self._sandbox(sandbox_id)
        to_state = self._success_target(sb["observed_state"], "lease_expiry")
        if to_state is None:
            raise RuntimeError(
                f"no lease_expiry transition from {sb['observed_state']}")
        sb["observed_state"] = to_state
        sb["reconciled"] = False
        sb["version"] += 1
        return self._sandbox_view(sb)

    def mark_reconciled(self, sandbox_id):
        """Simulate fencing/reconciliation completing (Error/Lost retry gate)."""
        sb = self._sandbox(sandbox_id)
        sb["reconciled"] = True
        if sb["pending_operation"]:
            op = self.operations[sb["pending_operation"]]
            if op["state"] in {"pending", "running"}:
                # reconciliation resolves the outstanding operation's outcome;
                # a timeout never implied not-executed, this is the verdict
                op["state"] = "failed"
                op["error"] = {"code": "lost_reconciled",
                               "message": "lease expired; outcome resolved by reconciliation"}
                op["updated_at"] = self._tick()
            sb["pending_operation"] = None
        return self._sandbox_view(sb)

    # ------------------------------------------------------------ persistence
    def snapshot(self):
        """Serializable state for restart simulation. Contains no secrets."""
        return {
            "clock": self._clock,
            "seq": self._seq,
            "sandboxes": copy.deepcopy(self.sandboxes),
            "operations": copy.deepcopy(self.operations),
            "idempotency": [dict(entry, key=key)
                            for key, entry in self.idempotency.items()],
        }

    def restore(self, snap):
        self._clock = snap["clock"]
        self._seq = snap["seq"]
        self.sandboxes = copy.deepcopy(snap["sandboxes"])
        self.operations = copy.deepcopy(snap["operations"])
        self.idempotency = {e["key"]: {k: v for k, v in e.items() if k != "key"}
                            for e in snap["idempotency"]}
        self._tickets = {}

    # ---------------------------------------------------------------- helpers
    def _authorized(self, headers):
        return headers.get("authorization") == f"Bearer {self.token}"

    def _sandbox(self, sid):
        sb = self.sandboxes.get(sid)
        if sb is None:
            raise _Refused(_err(404, "not_found", "sandbox not found"))
        return sb

    def _operation(self, oid):
        op = self.operations.get(oid)
        if op is None:
            raise _Refused(_err(404, "not_found", "operation not found"))
        return op

    def _tick(self):
        self._clock += 1
        return self._clock

    def _next_id(self, prefix):
        self._seq += 1
        return f"{prefix}_{self._seq:06d}"

    def _validated_resources(self, body):
        resources = body.get("resources")
        if not isinstance(resources, dict):
            raise _Refused(_err(422, "invalid", "resources object required"))
        out = {}
        for key in ("milli_cpu", "memory_bytes", "volume_bytes"):
            value = resources.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise _Refused(_err(422, "invalid",
                                    f"resources.{key} must be a positive integer"))
            out[key] = value
        return out

    def _trigger_for(self, observed, target):
        if target == "Suspend":
            return "suspend"
        if target == "Idle":
            return "set_idle"
        return "set_active" if observed == "Idle" else "resume"

    def _success_target(self, observed, trigger):
        """Success target of a transition pair; Error is the failure branch."""
        targets = [t for t in self._transitions.get((observed, trigger), [])
                   if t != "Error"]
        return targets[0] if targets else None

    def _state_refusal(self, sb, target):
        observed = sb["observed_state"]
        if observed == "Destroyed":
            return (409, "opposite_operation",
                    "Destroyed is terminal; repeat destroy returns the existing operation")
        if observed in {"Creating", "Suspending", "Resuming", "Destroying"}:
            return (409, "opposite_operation",
                    f"no state change accepted while {observed}")
        if observed in {"Error", "Lost"} and not sb["reconciled"]:
            return (409, "opposite_operation",
                    f"{observed} requires reconciliation/fencing before changes")
        return None

    def _apply_create_observation(self, sb):
        sb["observed_state"] = self.initial_state  # Creating until Runner evidence

    def _version_ok(self, sb, expected):
        return expected == sb["version"]

    def _fingerprint(self, body):
        """Canonical request body for idempotency; credential never participates."""
        stripped = {k: v for k, v in body.items() if k != "credential"}
        return json.dumps(stripped, sort_keys=True, separators=(",", ":"))

    def _idempotent_lookup(self, key):
        return self.idempotency.get(key)

    def _store_credential(self, sb, op, credential):
        # secrets never enter the sandbox record, operation payload or snapshot
        sb["has_credential"] = bool(credential)

    def _reserved_compute(self):
        out = {"milli_cpu": 0, "memory_bytes": 0}
        for sb in self.sandboxes.values():
            if sb["observed_state"] in COMPUTE_HELD_STATES:
                out["milli_cpu"] += sb["resources"]["milli_cpu"]
                out["memory_bytes"] += sb["resources"]["memory_bytes"]
        return out

    def _capacity_left(self):
        reserved = self._reserved_compute()
        volume = sum(sb["resources"]["volume_bytes"]
                     for sb in self.sandboxes.values()
                     if sb["observed_state"] != "Destroyed")
        return {"milli_cpu": self.capacity["milli_cpu"] - reserved["milli_cpu"],
                "memory_bytes": self.capacity["memory_bytes"] - reserved["memory_bytes"],
                "volume_bytes": self.capacity["volume_bytes"] - volume}

    def _new_operation(self, sb, op_type, trigger, target):
        op = {
            "operation_id": self._next_id("op"),
            "sandbox_id": sb["sandbox_id"],
            "type": op_type,
            "trigger": trigger or op_type,
            "target": target if target is not None else
                ({"agent": "claude", "resources": dict(sb["resources"])}),
            "state": "pending",
            "expected_version": sb["version"] if op_type != "create" else None,
            "generation": sb["generation"],
            "result": None,
            "error": None,
            "created_at": self._tick(),
            "updated_at": self._clock,
        }
        self.operations[op["operation_id"]] = op
        sb["pending_operation"] = op["operation_id"]
        return op

    def _idempotency_check(self, method, path, headers, body):
        """Return the stored entry for a replay, or None; refuse key conflicts."""
        key = headers.get("idempotency-key")
        if not key:
            raise _Refused(_err(422, "invalid", "Idempotency-Key header required"))
        entry = self._idempotent_lookup(key)
        if entry is None:
            return None
        if (entry["method"] != method or entry["path"] != path
                or entry["fingerprint"] != self._fingerprint(body)):
            raise _Refused(_err(409, "body_conflict",
                                "Idempotency-Key replayed with a different body"))
        return entry

    def _remember(self, key, method, path, body, status, op):
        if not key:
            return
        self.idempotency[key] = {
            "method": method,
            "path": path,
            "fingerprint": self._fingerprint(body),
            "status": status,
            "operation_id": op["operation_id"],
            "sandbox_id": op["sandbox_id"],
        }

    def _idempotent_replay(self, entry):
        op = self.operations[entry["operation_id"]]
        if entry["method"] == "POST" and entry["path"] == "/v1/sandboxes":
            body = {"sandbox_id": entry["sandbox_id"],
                    "operation": self._op_view(op)}
        else:
            body = {"operation": self._op_view(op)}
        return entry["status"], body

    def _op_view(self, op):
        return {
            "operation_id": op["operation_id"],
            "sandbox_id": op["sandbox_id"],
            "type": op["type"],
            "target": copy.deepcopy(op["target"]),
            "state": op["state"],
            "expected_version": op["expected_version"],
            "generation": op["generation"],
            "result": copy.deepcopy(op["result"]),
            "error": copy.deepcopy(op["error"]),
            "retryable": op["state"] == "failed",
            "created_at": op["created_at"],
            "updated_at": op["updated_at"],
        }

    def _sandbox_view(self, sb):
        view = {
            "sandbox_id": sb["sandbox_id"],
            "workspace_id": sb["workspace_id"],
            "desired_state": sb["desired_state"],
            "observed_state": sb["observed_state"],
            "generation": sb["generation"],
            "version": sb["version"],
            "pending_operation": (self._op_view(self.operations[sb["pending_operation"]])
                                  if sb["pending_operation"] else None),
            "last_confirmed_at": sb["last_confirmed_at"],
            "volumes": dict(sb["volumes"]),
            "runtime_deadline_at": sb["runtime_deadline_at"],
            "session": copy.deepcopy(sb["session"]),
        }
        if sb["observed_state"] == "Error":
            view["last_confirmed_state"] = sb["last_confirmed_state"]
            view["error"] = copy.deepcopy(sb["error"])
        if sb["observed_state"] in {"Active", "Idle"}:
            view["hold"] = None
            view["next_transition_at"] = None
        return view
