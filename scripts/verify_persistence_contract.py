#!/usr/bin/env python3
"""Check the workspace persistence contract against rules distilled from the ADRs.

Validates the internal consistency of docs/contracts/workspace-persistence.json.
It verifies a JSON preservation matrix, not a running system: no volume,
snapshot or runtime behavior is claimed.
"""
import json
from pathlib import Path
import sys

KNOWN_RESOURCES = {"sandbox_runtime", "workspace_volume", "home_volume", "snapshot"}
KNOWN_EVENTS = {
    "cold_suspend", "resume", "destroy", "memory_termination_recreation",
    "volume_resize", "snapshot_ttl_expiry",
}
REQUIRED_INVARIANTS = {
    "volume_lifecycle_independent_from_sandbox_state",
    "destroy_does_not_imply_snapshot_deletion",
    "retention_deadline_and_release_both_recorded",
    "suspend_keeps_storage_meters_running",
}
# Which resources each event must preserve / drop (ADR #5 preservation matrix,
# usage-ledger #2, memory-session-recovery #67).
EVENT_RULES = {
    "cold_suspend": {
        "must_preserve": {"workspace_volume", "home_volume"},
        "must_drop": {"sandbox_runtime"},
    },
    "resume": {
        "must_preserve": {"workspace_volume", "home_volume"},
        "must_drop": {"sandbox_runtime"},
    },
    "destroy": {
        "must_preserve": {"snapshot"},
        "must_drop": {"sandbox_runtime", "workspace_volume", "home_volume"},
    },
    "memory_termination_recreation": {
        "must_preserve": {"workspace_volume", "home_volume"},
        "must_drop": {"sandbox_runtime"},
    },
    "volume_resize": {
        "must_preserve": set(KNOWN_RESOURCES),
        "must_drop": set(),
    },
    "snapshot_ttl_expiry": {
        "must_preserve": {"sandbox_runtime", "workspace_volume", "home_volume"},
        "must_drop": {"snapshot"},
    },
}
REQUIRED_COLD_RESTART = {"no_reclone_no_overwrite", "session_id_stable", "cwd_preserved"}


def check_contract(doc):
    """Return a list of violation messages; empty means the contract is consistent."""
    violations = []

    def bad(msg):
        violations.append(msg)

    for key in ("schema_version", "title", "source_of_truth", "resources",
                "events", "invariants", "cold_restart"):
        if key not in doc:
            bad(f"missing top-level key: {key}")
    if violations:
        return violations

    if not (isinstance(doc["source_of_truth"], list) and doc["source_of_truth"]
            and all(isinstance(s, str) and s for s in doc["source_of_truth"])):
        bad("source_of_truth must be a non-empty list of strings")

    resources = doc["resources"]
    if not isinstance(resources, dict):
        return ["resources must be an object"]
    if set(resources) != KNOWN_RESOURCES:
        bad(f"resources must be exactly {sorted(KNOWN_RESOURCES)}; "
            f"got {sorted(set(resources))}")
    for name, meta in resources.items():
        if not isinstance(meta, dict) \
                or not (isinstance(meta.get("resource_id"), str) and meta["resource_id"]) \
                or not (isinstance(meta.get("aspects"), list) and meta["aspects"]) \
                or not all(isinstance(a, str) and a for a in meta.get("aspects", [])):
            bad(f"resource {name}: needs non-empty resource_id and a non-empty aspects list")
        elif len(set(meta["aspects"])) != len(meta["aspects"]):
            bad(f"resource {name}: duplicate aspects")

    events = doc["events"]
    if not isinstance(events, list) or not events:
        return ["events must be a non-empty list"]
    seen_ids = set()
    for i, event in enumerate(events):
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            bad(f"event[{i}]: needs a string id")
            continue
        eid = event["id"]
        where = f"event {eid!r}"
        if eid in seen_ids:
            bad(f"{where}: duplicate event id")
        seen_ids.add(eid)
        if eid not in KNOWN_EVENTS:
            bad(f"{where}: unknown event id (closed world: {sorted(KNOWN_EVENTS)})")
        if not (isinstance(event.get("trigger"), str) and event["trigger"]):
            bad(f"{where}: needs a non-empty trigger")
        guarded = event.get("guarded_by")
        if not isinstance(guarded, list) or not guarded \
                or not all(isinstance(g, str) and g for g in guarded):
            bad(f"{where}: guarded_by must be a non-empty list of acceptance-check strings")

        preserves, drops = event.get("preserves"), event.get("drops")
        for key, value in (("preserves", preserves), ("drops", drops)):
            if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
                bad(f"{where}: {key} must be a list of resource names")
                preserves = drops = None
                break
        if preserves is None:
            continue
        for key, value in (("preserves", preserves), ("drops", drops)):
            for name in value:
                if name not in KNOWN_RESOURCES:
                    bad(f"{where}: {key} names unknown resource {name!r}")
        overlap = sorted(set(preserves) & set(drops))
        if overlap:
            bad(f"{where}: resource(s) {overlap} are both preserved and dropped "
                f"in the same event; each decision must be defined exactly once")
        undecided = sorted(KNOWN_RESOURCES - set(preserves) - set(drops))
        if undecided:
            bad(f"{where}: resource(s) {undecided} have no preserve/drop decision; "
                f"every resource needs exactly one decision per event")

        details = event.get("details")
        if not isinstance(details, dict) or set(details) != KNOWN_RESOURCES:
            bad(f"{where}: details must cover exactly {sorted(KNOWN_RESOURCES)}")
        elif not all(isinstance(d, str) and d for d in details.values()):
            bad(f"{where}: every detail must be a non-empty string")

        if eid in EVENT_RULES:
            rules = EVENT_RULES[eid]
            for name in sorted(rules["must_preserve"] - set(preserves)):
                why = "suspend/recreation keeps volumes" if name.endswith("volume") \
                    else "independent lifecycle, kept alive"
                bad(f"{where}: must preserve {name} ({why})")
            for name in sorted(rules["must_drop"] - set(drops)):
                why = "no carry-over of RAM/processes/credentials" if name == "sandbox_runtime" \
                    else "confirmed data deletion"
                bad(f"{where}: must drop {name} ({why})")
    for eid in sorted(KNOWN_EVENTS - seen_ids):
        bad(f"event {eid!r} from the preservation matrix is missing from the contract")

    # Recreation preserves the recorded session ID/cwd with the home volume (#67).
    for event in events:
        if isinstance(event, dict) and event.get("id") == "memory_termination_recreation" \
                and isinstance(event.get("details"), dict):
            home = event["details"].get("home_volume", "")
            if "session" not in home:
                bad("event 'memory_termination_recreation': home_volume detail must state "
                    "the recorded session ID/cwd is preserved")

    invariants = doc["invariants"]
    if not isinstance(invariants, list) or not invariants:
        return ["invariants must be a non-empty list"]
    ids = [inv.get("id") for inv in invariants if isinstance(inv, dict)]
    if len(ids) != len(invariants):
        bad("every invariant needs an id")
    if len(set(ids)) != len(ids):
        bad("duplicate invariant ids")
    for inv in invariants:
        if isinstance(inv, dict) and not (isinstance(inv.get("statement"), str) and inv["statement"]):
            bad(f"invariant {inv.get('id')!r}: needs a non-empty statement")
    for rule in sorted(REQUIRED_INVARIANTS - set(ids)):
        bad(f"required invariant {rule} is missing")

    cold = doc["cold_restart"]
    if not isinstance(cold, dict):
        bad("cold_restart must be an object")
    else:
        for key in REQUIRED_COLD_RESTART:
            if not (isinstance(cold.get(key), str) and cold[key]):
                bad(f"cold_restart.{key} must be a non-empty expectation")
        noreclone = cold.get("no_reclone_no_overwrite")
        if isinstance(noreclone, str) and "docs/contracts/work-image.md" not in noreclone:
            bad("cold_restart.no_reclone_no_overwrite must reference the work-image contract "
                "(docs/contracts/work-image.md)")
        session = cold.get("session_id_stable")
        if isinstance(session, str) and "docker start" not in session:
            bad("cold_restart.session_id_stable must require recreation evidence, "
                "not a same-container docker start")

    return violations


if __name__ == "__main__":
    source = Path(__file__).resolve().parents[1] / "docs/contracts/workspace-persistence.json"
    doc = json.loads(source.read_text())
    violations = check_contract(doc)
    for message in violations:
        print("FAIL", message, file=sys.stderr)
    if violations:
        sys.exit(1)
    n = len(doc["events"]) * len(doc["resources"])
    print(f"{len(doc['events'])} events x {len(doc['resources'])} resources = {n} decisions, "
          f"{len(doc['invariants'])} invariants: consistent with docs/adr/sandbox-lifecycle.md #5, "
          f"memory-session-recovery.md and usage-ledger.md #2 "
          f"(contract check only, no runtime verification claimed).")
