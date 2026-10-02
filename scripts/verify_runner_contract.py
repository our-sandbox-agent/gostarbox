#!/usr/bin/env python3
"""Check the runner lifecycle contract against rules distilled from the ADRs.

Validates the internal consistency of docs/contracts/runner-lifecycle.json.
It verifies a JSON table, not a running system: no runtime behavior is claimed.
"""
import json
from pathlib import Path
import re
import sys

KNOWN_TRIGGERS = {
    "create", "suspend", "resume", "set_active", "set_idle", "destroy",
    "lease_expiry", "memory_termination_confirmed",
}
KNOWN_ERROR_CODES = {"memory_limit_terminated", "recovery_retry_exhausted"}
KNOWN_REFUSAL_CODES = {"capacity_exceeded", "credentials_required", "unsupported_suspend_mode"}
REQUIRED_INVARIANTS = {
    "destroyed_is_terminal", "lost_keeps_reservation", "lost_not_direct_destroyed",
    "generation_fencing", "observed_not_desired",
}
ERROR_EXITS = {"Resuming", "Destroying", "Lost"}


def reachable(transitions, start):
    seen, frontier = {start}, [start]
    while frontier:
        state = frontier.pop()
        for t in transitions:
            if t.get("from") == state and t.get("to") not in seen:
                seen.add(t.get("to"))
                frontier.append(t.get("to"))
    return seen


def check_contract(doc):
    """Return a list of violation messages; empty means the contract is consistent."""
    violations = []

    def bad(msg):
        violations.append(msg)

    for key in ("initial_state", "states", "transitions", "refusals",
                "capacity_admission", "invariants"):
        if key not in doc:
            bad(f"missing top-level key: {key}")
    if violations:
        return violations

    states, transitions = doc["states"], doc["transitions"]
    if not isinstance(states, dict) or not states:
        return ["states must be a non-empty object"]
    if not isinstance(transitions, list) or not transitions:
        return ["transitions must be a non-empty list"]
    for name, meta in states.items():
        if not isinstance(meta, dict) or not isinstance(meta.get("terminal"), bool) \
                or not (isinstance(meta.get("entry"), str) and meta["entry"]):
            bad(f"state {name}: needs boolean terminal and non-empty entry")
    if doc["initial_state"] not in states:
        bad(f"initial_state {doc['initial_state']!r} is not a defined state")
    if "Active" not in states:
        bad("state Active must exist (operational reachability hub)")

    seen_pairs = set()
    for i, t in enumerate(transitions):
        where = f"transition[{i}] ({t.get('from')}->{t.get('to')})"
        for key in ("from", "to", "trigger"):
            if not isinstance(t.get(key), str):
                bad(f"{where}: missing string {key}")
        requires, effects = t.get("requires"), t.get("side_effects")
        if not isinstance(requires, list) or not requires \
                or not all(isinstance(r, str) and r for r in requires):
            bad(f"{where}: requires must be a non-empty list of strings")
        if not isinstance(effects, list) or not effects \
                or not all(isinstance(e, str) and e for e in effects):
            bad(f"{where}: side_effects must be a non-empty list of strings")
        if t.get("trigger") not in KNOWN_TRIGGERS:
            bad(f"{where}: unknown trigger {t.get('trigger')!r}")
        for key in ("from", "to"):
            if isinstance(t.get(key), str) and t[key] not in states:
                bad(f"{where}: {key} state {t[key]!r} is not defined")
        pair = (t.get("from"), t.get("to"), t.get("trigger"))
        if pair in seen_pairs:
            bad(f"{where}: duplicate transition on trigger {pair[2]!r}")
        seen_pairs.add(pair)
        if not isinstance(t.get("error_codes", []), list):
            bad(f"{where}: error_codes must be a list")
        for code in t.get("error_codes", []):
            if code not in KNOWN_ERROR_CODES:
                bad(f"{where}: error code {code!r} is not defined by the ADRs")

    # Terminal states have no exits; every other state must have at least one.
    for name, meta in states.items():
        exits = [t for t in transitions if t.get("from") == name]
        if meta.get("terminal") and exits:
            bad(f"terminal state {name} must have no outgoing transitions")
        if meta.get("terminal") is False and not exits:
            bad(f"non-terminal state {name} has no outgoing transition (dead end)")

    # Reachability: everything from the initial state; everything but the
    # create-only entry state also from Active (the operational hub).
    for name in sorted(set(states) - reachable(transitions, doc["initial_state"])):
        bad(f"state {name} is unreachable from initial state {doc['initial_state']}")
    if "Active" in states and not [m for m in violations if "Active must exist" in m]:
        for name in sorted(set(states) - {doc["initial_state"]} - reachable(transitions, "Active")):
            bad(f"state {name} is unreachable from Active")

    # Lost: fenced exits only, reservation kept, never straight to Destroyed.
    for i, t in enumerate(transitions):
        if t.get("from") != "Lost":
            continue
        where = f"transition[{i}] (Lost->{t.get('to')})"
        if t.get("to") == "Destroyed":
            bad(f"{where}: Lost must not go directly to Destroyed (fence and reconcile first)")
        if not any(re.search(r"fenc|reconcil", r, re.I)
                   for r in t.get("requires", []) if isinstance(r, str)):
            bad(f"{where}: requires must include fencing/reconciliation")
        if any(re.search(r"releas", e, re.I)
               for e in t.get("side_effects", []) if isinstance(e, str)):
            bad(f"{where}: Lost transitions must not release capacity (reservation is kept)")

    # Error discipline: evidence kept on entry; recovery only via Resuming/Destroying/Lost.
    for i, t in enumerate(transitions):
        if t.get("to") != "Error":
            continue
        where = f"transition[{i}] ({t.get('from')}->Error)"
        text = " ".join(t.get("side_effects", []))
        if "last_confirmed_state" not in text:
            bad(f"{where}: must record last_confirmed_state")
        if "residual resources" not in text:
            bad(f"{where}: must list residual resources; capacity released only after confirmed release")
    for i, t in enumerate(transitions):
        if t.get("from") == "Error" and t.get("to") not in ERROR_EXITS:
            bad(f"transition[{i}]: Error must not go directly to {t.get('to')} "
                f"(recovery goes through Resuming after reconcile; never silently Idle)")
    memory_rows = [t for t in transitions if "memory_limit_terminated" in t.get("error_codes", [])]
    if not memory_rows:
        bad("error code memory_limit_terminated from ADR #67 is missing from the contract")
    for i, t in enumerate(memory_rows):
        if t.get("to") != "Error" or t.get("trigger") != "memory_termination_confirmed":
            bad(f"transition[{i}]: memory_limit_terminated only applies to ->Error "
                f"on trigger memory_termination_confirmed")
    if not any(t.get("from") == "Resuming" and t.get("to") == "Error"
               and "recovery_retry_exhausted" in t.get("error_codes", []) for t in transitions):
        bad("error code recovery_retry_exhausted from ADR #67 is missing on Resuming->Error")

    # Refusals: explicit, closed-world, never create operations.
    seen_ids, seen_codes = set(), set()
    for i, r in enumerate(doc["refusals"]):
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r.get("id"):
            bad(f"refusal[{i}]: needs a non-empty id")
            continue
        where = f"refusal {r['id']!r}"
        if r["id"] in seen_ids:
            bad(f"{where}: duplicate refusal id")
        seen_ids.add(r["id"])
        if not (isinstance(r.get("when"), str) and r["when"]):
            bad(f"{where}: needs a non-empty when")
        if r.get("http_status") is not None and r.get("http_status") not in (409, 422):
            bad(f"{where}: http_status must be 409, 422 or null")
        code = r.get("code")
        if code is not None and code not in KNOWN_REFUSAL_CODES:
            bad(f"{where}: refusal code {code!r} is not defined by the ADRs")
        if r.get("creates_operation") is not False:
            bad(f"{where}: a refusal must not create an operation (creates_operation must be false)")
        if code:
            seen_codes.add(code)
    for code in sorted(KNOWN_REFUSAL_CODES - seen_codes):
        bad(f"refusal code {code!r} from the ADR is missing from the contract")

    # Capacity admission guard inputs.
    cap = doc["capacity_admission"]
    if not isinstance(cap, dict):
        bad("capacity_admission must be an object")
    else:
        checked = cap.get("checked_on")
        if not isinstance(checked, list) \
                or not {"create", "resume"} <= set(checked) <= KNOWN_TRIGGERS:
            bad("capacity_admission.checked_on must cover at least create and resume with known triggers")
        inputs = cap.get("inputs")
        if not isinstance(inputs, dict) or set(inputs) != {"milli_cpu", "memory_bytes", "volume_bytes"}:
            bad("capacity_admission.inputs must be exactly milli_cpu, memory_bytes, volume_bytes")
        exceeded = cap.get("on_exceeded")
        if not isinstance(exceeded, dict) or exceeded.get("code") != "capacity_exceeded" \
                or exceeded.get("http_status") != 409 \
                or exceeded.get("creates_operation") is not False \
                or exceeded.get("transition") is not None:
            bad("capacity_admission.on_exceeded must be 409 capacity_exceeded "
                "with no operation and no transition")

    # Invariants: the terminal/fencing rules the ADR names must be present.
    ids = [inv.get("id") for inv in doc["invariants"] if isinstance(inv, dict)]
    if len(ids) != len(doc["invariants"]):
        bad("every invariant needs an id")
    if len(set(ids)) != len(ids):
        bad("duplicate invariant ids")
    for rule in sorted(REQUIRED_INVARIANTS - set(ids)):
        bad(f"required invariant {rule} is missing")

    return violations


if __name__ == "__main__":
    source = Path(__file__).resolve().parents[1] / "docs/contracts/runner-lifecycle.json"
    doc = json.loads(source.read_text())
    violations = check_contract(doc)
    for message in violations:
        print("FAIL", message, file=sys.stderr)
    if violations:
        sys.exit(1)
    print(f"{len(doc['transitions'])} transitions across {len(doc['states'])} states, "
          f"{len(doc['refusals'])} refusals: consistent with docs/adr/sandbox-lifecycle.md "
          f"(contract check only, no runtime verification claimed).")
