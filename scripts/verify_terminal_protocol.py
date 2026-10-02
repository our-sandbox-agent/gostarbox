#!/usr/bin/env python3
"""Check the terminal protocol contract against rules distilled from issue #13 and the ADRs.

Validates the internal consistency of docs/contracts/terminal-protocol.json.
It verifies a JSON message table, not a running system: no runtime behavior is claimed.
"""
import json
from pathlib import Path
import re
import sys

KNOWN_FRAMES = {
    "hello": {"token", "sandbox_id", "requested_size"},
    "hello_ack": {"generation", "cols", "rows"},
    "hello_err": {"code", "message"},
    "input": {"data"},
    "output": {"data"},
    "resize": {"cols", "rows"},
    "resized": {"cols", "rows", "source"},
    "ping": set(),
    "pong": {"ping_seq"},
    "bye": {"reason"},
}
KNOWN_ERROR_CODES = {
    "auth_failed", "sandbox_not_found", "sandbox_not_active", "wrong_generation",
    "backpressure_overflow", "output_limit_exceeded", "invalid_frame", "rate_limited",
}
DIRECTIONS = {"client_to_server", "server_to_client", "both"}
REQUIRED_TOP_KEYS = ("status", "transport", "common_fields", "frames", "error_codes",
                     "semantics", "activity_classification", "lifecycle_binding",
                     "backpressure", "related_tickets", "open_decisions")
SIZE_MIN, SIZE_MAX = 1, 500
REQUIRED_TICKETS = {13, 19, 23}
FORBIDDEN_CAPABILITY = re.compile(r"record|scrollback|playback", re.I)
TICKET_REF = re.compile(r"#(\d+)")


def iter_strings(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from iter_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_strings(item)
    elif isinstance(node, str):
        yield node


def check_contract(doc):
    """Return a list of violation messages; empty means the contract is consistent."""
    violations = []

    def bad(msg):
        violations.append(msg)

    for key in REQUIRED_TOP_KEYS:
        if key not in doc:
            bad(f"missing top-level key: {key}")
    if violations:
        return violations
    if doc["status"] != "Proposed":
        bad("status must stay Proposed until runtime integration lands under #13")

    # Transport stays backend-agnostic; the backend choice stays explicitly OPEN.
    if doc["transport"].get("transport_agnostic") is not True:
        bad("transport.transport_agnostic must be true (backend choice must not change the frames)")
    if "WebSocket" not in doc["transport"].get("binding", ""):
        bad("transport.binding must name the WebSocket frame carrier")
    decision = next((d for d in doc["open_decisions"]
                     if isinstance(d, dict) and d.get("id") == "backend-ttyd-vs-own-pty"), None)
    if decision is None or decision.get("status") != "OPEN":
        bad("open_decisions must keep backend-ttyd-vs-own-pty explicitly OPEN")

    # Every frame carries seq and ts.
    for field in ("seq", "ts"):
        if field not in doc["common_fields"]:
            bad(f"common_fields must define {field} (every frame carries seq and ts)")

    # Frames: closed set, unique names, per-type required fields.
    frames = doc["frames"]
    if not isinstance(frames, list) or not frames:
        return ["frames must be a non-empty list"]
    names = []
    for i, f in enumerate(frames):
        if not isinstance(f, dict) or not isinstance(f.get("name"), str) or not f.get("name"):
            bad(f"frame[{i}]: needs a non-empty name")
            continue
        names.append(f["name"])
        where = f"frame {f['name']}"
        if f.get("direction") not in DIRECTIONS:
            bad(f"{where}: direction must be one of {sorted(DIRECTIONS)}")
        for key in ("required_fields", "optional_fields"):
            if not isinstance(f.get(key), list) \
                    or not all(isinstance(x, str) and x for x in f[key]):
                bad(f"{where}: {key} must be a list of non-empty strings")
        if not (isinstance(f.get("purpose"), str) and f["purpose"]):
            bad(f"{where}: needs a non-empty purpose")
        req, opt = set(f.get("required_fields", [])), set(f.get("optional_fields", []))
        if req & opt:
            bad(f"{where}: required_fields and optional_fields overlap")
    for n in sorted({n for n in names if names.count(n) > 1}):
        bad(f"duplicate frame name {n!r}")
    for n in sorted(set(names) - set(KNOWN_FRAMES)):
        bad(f"unknown frame type {n!r}; the closed frame set is defined by issue #13")
    for n in sorted(set(KNOWN_FRAMES) - set(names)):
        bad(f"required frame type {n!r} is missing")
    for f in frames:
        if isinstance(f, dict) and f.get("name") in KNOWN_FRAMES \
                and set(f.get("required_fields", [])) != KNOWN_FRAMES[f["name"]]:
            bad(f"frame {f['name']}: required_fields must be exactly "
                f"{sorted(KNOWN_FRAMES[f['name']])}")
    frame_names = set(names)

    # Error codes: closed set, each carried by a defined frame.
    entries = doc["error_codes"]
    if not isinstance(entries, list) or not entries:
        return ["error_codes must be a non-empty list"]
    codes = [e.get("code") for e in entries]
    if len(set(codes)) != len(codes):
        bad("duplicate error code")
    for code in sorted(set(codes) - KNOWN_ERROR_CODES):
        bad(f"error code {code!r} is outside the closed set from issue #13")
    for code in sorted(KNOWN_ERROR_CODES - set(codes)):
        bad(f"error code {code!r} from issue #13 is missing")
    for e in entries:
        where = f"error code {e.get('code')!r}"
        words = set(re.findall(r"\w+", e.get("carrier", "")))
        if not isinstance(e.get("carrier"), str) or not (frame_names & words):
            bad(f"{where}: carrier must name a defined frame")
        if not isinstance(e.get("fatal"), bool):
            bad(f"{where}: fatal must be a boolean")
        if not (isinstance(e.get("client_action"), str) and e["client_action"]):
            bad(f"{where}: needs a non-empty client_action")

    # Tickets: positive integers, linked owners, every #N string reference covered.
    tickets = []
    for t in doc["related_tickets"]:
        n = t.get("ticket") if isinstance(t, dict) else None
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            bad("related_tickets entries must carry a positive integer ticket")
        else:
            tickets.append(n)
    if len(set(tickets)) != len(tickets):
        bad("duplicate ticket entry")
    for n in sorted(REQUIRED_TICKETS - set(tickets)):
        bad(f"ticket #{n} must be linked (owner or activity consumers)")
    refs = {int(m) for m in TICKET_REF.findall(" ".join(iter_strings(doc)))}
    for n in sorted(refs - set(tickets)):
        bad(f"string reference #{n} has no integer entry in related_tickets")

    # Size policy: sane bounds, explicit Proposed two-window rule.
    size = doc["semantics"].get("size_policy")
    if not isinstance(size, dict):
        return ["semantics.size_policy must be an object"]
    bounds = size.get("bounds", {})
    for axis in ("cols", "rows"):
        b = bounds.get(axis)
        if not isinstance(b, dict) \
                or not all(isinstance(b.get(k), int) and not isinstance(b.get(k), bool)
                           for k in ("min", "max")):
            bad(f"size_policy.bounds.{axis} needs integer min and max")
            continue
        if not SIZE_MIN <= b["min"] <= b["max"] <= SIZE_MAX:
            bad(f"size_policy.bounds.{axis} must stay within {SIZE_MIN}..{SIZE_MAX}")
    multi = size.get("multi_window", {})
    if multi.get("policy") != "last_resize_wins":
        bad("size_policy.multi_window.policy must be last_resize_wins (explicit two-window decision)")
    if multi.get("notification_frame") != "resized":
        bad("size_policy.multi_window must notify both windows via the resized frame")
    if "Proposed" not in size.get("status", ""):
        bad("size_policy.status must mark the two-window decision as Proposed")

    # Backpressure: bounded buffer, explicit overflow action, fatal cap, exit path.
    bp = doc["backpressure"]
    if not isinstance(bp, dict):
        return ["backpressure must be an object"]
    if bp.get("buffer", {}).get("bounded") is not True:
        bad("backpressure.buffer.bounded must be true; the server-side output buffer is never unbounded")
    overflow = bp.get("on_overflow", {})
    actions = overflow.get("actions", [])
    if overflow.get("mode") != "drop_to_sync":
        bad("backpressure.on_overflow.mode must be drop_to_sync")
    if not isinstance(actions, list) or not actions:
        bad("backpressure.on_overflow.actions must be a non-empty list")
    if not any(isinstance(a, str) and "output_limit_exceeded" in a for a in actions):
        bad("backpressure.on_overflow must carry the explicit output_limit_exceeded notice")
    if "backpressure_overflow" not in json.dumps(bp.get("fatal_cap", {})):
        bad("backpressure must define the fatal cap with code backpressure_overflow")
    if not (isinstance(bp.get("exit_drop_to_sync"), str) and bp["exit_drop_to_sync"]):
        bad("backpressure must define how drop_to_sync exits")

    # Activity classification: only input is user activity; separate byte counters.
    ac = doc["activity_classification"]
    if not isinstance(ac, dict):
        return ["activity_classification must be an object"]
    classes = ac.get("classes", {})
    if not isinstance(classes, dict) or not classes:
        return ["activity_classification.classes must be a non-empty object"]
    assigned = {}
    for cname, cdef in classes.items():
        flist = cdef.get("frames") if isinstance(cdef, dict) else None
        if not isinstance(flist, list) or not flist:
            bad(f"activity class {cname} must list its frames")
            continue
        for fname in flist:
            if fname in assigned:
                bad(f"frame {fname!r} is classified under both {assigned[fname]!r} and {cname!r}")
            assigned[fname] = cname
    for fname in sorted(frame_names - set(assigned)):
        bad(f"frame {fname!r} is not classified in activity_classification")
    ui = classes.get("user_input", {}).get("frames")
    if ui != ["input"]:
        bad("activity class user_input must be exactly ['input'] "
            "(ping, resize and output never count as user activity)")
    for fname in ("ping", "pong", "resize", "resized", "output"):
        if isinstance(ui, list) and fname in ui:
            bad(f"{fname} must never be classified as user input")
    for cname, cdef in classes.items():
        if cname != "user_input" and isinstance(cdef, dict) \
                and cdef.get("counts_as_user_activity") is not False:
            bad(f"activity class {cname} must set counts_as_user_activity false")
    purpose = ac.get("purpose", "")
    for ticket in ("#19", "#23"):
        if ticket not in purpose:
            bad(f"activity_classification.purpose must link {ticket}")
    counters = ac.get("counters", {})
    for cname in ("user_input_frames", "user_input_bytes",
                  "output_frames", "output_bytes"):
        if cname not in counters:
            bad(f"activity counters must include {cname} (input and output counted separately)")
    if counters.get("user_input_bytes") == counters.get("output_bytes"):
        bad("input and output byte counters must be separate entries with separate meanings")

    # Lifecycle: bye-then-close, detach keeps tmux, destroy and generation guards.
    lb = doc["lifecycle_binding"]
    if not isinstance(lb, dict):
        return ["lifecycle_binding must be an object"]
    rule = lb.get("on_stop_or_destroy", {}).get("rule", "")
    low = rule.lower()
    if "bye" not in low or "clos" not in low or low.find("bye") > low.find("clos"):
        bad("on stop/destroy the server must send bye and only then close (bye-then-close ordering)")
    if lb.get("reconnect_after_destroy", {}).get("code") != "sandbox_not_found":
        bad("reconnect after destroy must answer sandbox_not_found")
    if lb.get("wrong_generation_guard", {}).get("code") != "wrong_generation":
        bad("wrong_generation guard is missing from lifecycle_binding")
    na = lb.get("not_active", {})
    if na.get("code") != "sandbox_not_active" or "Suspend" not in na.get("note", ""):
        bad("sandbox_not_active must cover every non-running state including Suspend")
    detach = lb.get("detach", {}).get("statement", "")
    if "tmux" not in detach.lower() or "never kills" not in detach.lower():
        bad("detach must state it never kills the tmux session or its processes")

    # Reconnect replay comes from a live capture, never a kept content buffer.
    reconnect = doc["semantics"].get("reconnect", {})
    if "tmux" not in reconnect.get("screen_replay", "").lower():
        bad("reconnect screen replay must come from a tmux capture")
    if not reconnect.get("no_content_buffer"):
        bad("reconnect must state there is no server-side terminal content buffer")

    # No terminal-content capture feature anywhere (issue #13: 不錄製終端).
    for text in iter_strings(doc):
        m = FORBIDDEN_CAPABILITY.search(text)
        if m:
            bad(f"forbidden terminal-content capture feature {m.group(0)!r}; issue #13 excludes it")
            break

    return violations


if __name__ == "__main__":
    source = Path(__file__).resolve().parents[1] / "docs/contracts/terminal-protocol.json"
    doc = json.loads(source.read_text())
    violations = check_contract(doc)
    for message in violations:
        print("FAIL", message, file=sys.stderr)
    if violations:
        sys.exit(1)
    classes = len(doc["activity_classification"]["classes"])
    print(f"{len(doc['frames'])} frame types, {len(doc['error_codes'])} error codes, "
          f"{classes} activity classes, {len(doc['related_tickets'])} linked tickets: "
          f"consistent with issue #13 and the ADRs "
          f"(contract check only, no runtime verification claimed).")
