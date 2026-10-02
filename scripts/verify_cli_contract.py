#!/usr/bin/env python3
"""Check the CLI surface contract against rules distilled from issue #14, plan.md and the ADRs.

Validates the internal consistency of docs/contracts/cli-surface.json.
It verifies a JSON argv surface, not a running binary: no runtime behavior is
claimed (the Go implementation is pending toolchain plus #12/#13 runtime).
"""
import json
from pathlib import Path
import re
import sys

KNOWN_COMMANDS = {"login", "claude", "ls", "connect", "suspend", "destroy", "exec", "cp"}
KNOWN_UNSUPPORTED = {"snapshot", "fork", "resume"}
REQUIRED_EXIT_CODES = {0, 64, 65, 68, 69, 70, 75, 76}
REQUIRED_TOP_KEYS = ("status", "binary", "control_plane", "global_flags", "commands",
                     "unsupported_commands", "exit_codes", "token", "terminal",
                     "telemetry", "install_artifacts", "api_gaps", "related_tickets")
REQUIRED_TICKETS = {12, 13, 14, 20, 76}
RESTORE_PATHS = {"normal exit", "Ctrl-\\ detach", "SIGINT/SIGTERM", "socket close", "panic"}
REQUIRED_TELEMETRY_FIELDS = {"terminal_ready_ms", "clone_done_ms"}
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


def iter_flag_names(doc):
    for f in doc.get("global_flags", []):
        if isinstance(f, dict):
            yield f.get("flag")
    for c in doc.get("commands", []):
        if isinstance(c, dict) and isinstance(c.get("flags"), list):
            for f in c["flags"]:
                yield f.get("flag") if isinstance(f, dict) else f


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
        bad("status must stay Proposed until the Go CLI runtime lands under #14")

    # Binary stays a pending Go implementation; no runtime claims.
    binary = doc["binary"]
    if "Go" not in binary.get("language", ""):
        bad("binary.language must name Go per docs/adr/runner-language.md (Accepted)")
    status_text = binary.get("implementation_status", "")
    if "not started" not in status_text or "no runtime claims" not in status_text:
        bad("binary.implementation_status must state the implementation is not started and make no runtime claims")

    # Control plane only: no flag may bypass it, every command routes through it.
    cp = doc["control_plane"]
    rule = cp.get("rule", "")
    if "control plane" not in rule or "never" not in rule.lower() or "Runner" not in rule:
        bad("control_plane.rule must state every command goes through the control plane, never a Runner")
    for flag in iter_flag_names(doc):
        if isinstance(flag, str) and "direct_runner" in flag:
            bad(f"flag {flag!r} bypasses the control plane; direct_runner flags are forbidden")

    # Commands: closed set, unique, usage + documented exit codes + control-plane routing.
    commands = doc["commands"]
    if not isinstance(commands, list) or not commands:
        return ["commands must be a non-empty list"]
    names = [c.get("name") for c in commands]
    if len(set(names)) != len(names):
        bad("duplicate command name")
    for n in sorted(set(names) - KNOWN_COMMANDS):
        bad(f"unknown command {n!r}; the closed command set is defined by issue #14 and plan.md")
    for n in sorted(KNOWN_COMMANDS - set(names)):
        bad(f"required command {n!r} is missing")
    documented = {e.get("code") for e in doc["exit_codes"]}
    for c in commands:
        where = f"command {c.get('name')!r}"
        if not (isinstance(c.get("usage"), str) and c["usage"].startswith("sandbox ")):
            bad(f"{where}: needs a usage string starting with 'sandbox '")
        if c.get("control_plane_only") is not True:
            bad(f"{where}: control_plane_only must be true (never direct to Runner)")
        codes = c.get("exit_codes")
        if not isinstance(codes, list) or not codes \
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in codes):
            bad(f"{where}: exit_codes must be a non-empty list of integers")
            continue
        for code in sorted(set(codes) - documented):
            bad(f"{where}: exit code {code} is not documented in exit_codes")
    by_name = {c["name"]: c for c in commands if isinstance(c, dict)}

    claude = by_name.get("claude", {})
    if "clone_args.py" not in claude.get("repo_validation", ""):
        bad("claude.repo_validation must reference the scripts/clone_args.py URL rule")

    exec_cmd = by_name.get("exec", {})
    if "--" not in exec_cmd.get("usage", ""):
        bad("exec usage must carry the -- literal-argv separator")
    if "never shell-parsed" not in exec_cmd.get("literal_argv_rule", ""):
        bad("exec.literal_argv_rule must state argv after -- is never shell-parsed")

    cp_cmd = by_name.get("cp", {})
    if cp_cmd.get("status") != "defined-interface":
        bad("cp.status must be defined-interface; the implementation lands with #20")
    if 20 not in (cp_cmd.get("depends_on") or []):
        bad("cp.depends_on must include ticket 20 (integration owner)")
    if "mock" not in cp_cmd.get("pre_implementation_behavior", ""):
        bad("cp.pre_implementation_behavior must state it never mocks success")

    login = by_name.get("login", {})
    if "no OAuth" not in login.get("no_oauth", ""):
        bad("login must state there is no OAuth flow in this ticket")

    # Unsupported commands: closed set, explicit template, non-zero exit.
    unsupported = doc["unsupported_commands"]
    if not isinstance(unsupported, list) or not unsupported:
        return ["unsupported_commands must be a non-empty list"]
    unames = [u.get("name") for u in unsupported]
    if len(set(unames)) != len(unames):
        bad("duplicate unsupported command name")
    for n in sorted(set(unames) - KNOWN_UNSUPPORTED):
        bad(f"unsupported entry {n!r} is outside the closed unsupported set from issue #14/plan.md")
    for n in sorted(set(unames) & set(names)):
        bad(f"command {n!r} is both supported and unsupported")
    for u in unsupported:
        where = f"unsupported command {u.get('name')!r}"
        code = u.get("exit_code")
        if not isinstance(code, int) or isinstance(code, bool) or code == 0:
            bad(f"{where}: exit_code must be a non-zero integer")
        elif code != 64:
            bad(f"{where}: unsupported commands exit 64 (EX_USAGE)")
        template = u.get("message_template", "")
        if "unsupported" not in template.lower() or "{command}" not in template:
            bad(f"{where}: message_template must say unsupported and carry the {{command}} placeholder")
    for n in sorted(KNOWN_UNSUPPORTED - set(unames)):
        bad(f"required unsupported entry {n!r} is missing")

    # Exit codes: closed set, unique, documented, detach and reconnect semantics.
    entries = doc["exit_codes"]
    if not isinstance(entries, list) or not entries:
        return ["exit_codes must be a non-empty list"]
    codes = [e.get("code") for e in entries]
    for code in codes:
        if not isinstance(code, int) or isinstance(code, bool) or not 0 <= code <= 255:
            bad(f"exit code {code!r} must be an integer in 0..255")
    if len(set(codes)) != len(codes):
        bad("duplicate exit code")
    for code in sorted(REQUIRED_EXIT_CODES - set(codes)):
        bad(f"exit code {code} from issue #14 is missing")
    for e in entries:
        if not (isinstance(e.get("meaning"), str) and e["meaning"]):
            bad(f"exit code {e.get('code')!r} needs a non-empty meaning")
    by_code = {e["code"]: e.get("meaning", "") for e in entries if isinstance(e, dict)}
    if "keeps running" not in by_code.get(0, ""):
        bad("exit code 0 must document the Ctrl-\\ detach exit (sandbox keeps running)")
    if "reconnect" not in by_code.get(76, "").lower():
        bad("exit code 76 must document exhausted auto-reconnect after a network drop")

    # Token discipline: one 0600 file, never argv/logs, env override for CI.
    token = doc["token"]
    if token.get("file_mode") != "0600":
        bad("token.file_mode must be 0600")
    if not (isinstance(token.get("path"), str) and token["path"]):
        bad("token.path must document the token file path")
    if token.get("never_in_argv") is not True or token.get("never_in_logs") is not True:
        bad("token must set never_in_argv and never_in_logs true")
    env = token.get("env_override", {})
    if env.get("name") != "SANDBOX_TOKEN":
        bad("token.env_override must be SANDBOX_TOKEN (CI)")
    if token.get("missing_token_exit") != 68:
        bad("token.missing_token_exit must be 68")

    # Terminal: raw mode restored on every exit path, detach, reconnect, cold notice.
    term = doc["terminal"]
    paths = term.get("raw_mode_restored_on_exit_paths")
    if not isinstance(paths, list) or set(paths) != RESTORE_PATHS:
        bad(f"raw mode must be restored on exactly the closed path set {sorted(RESTORE_PATHS)}")
    if term.get("detach_exit_code") != 0:
        bad("Ctrl-\\ detach must exit 0")
    if "keeps running" not in term.get("detach_semantics", ""):
        bad("detach_semantics must state the sandbox keeps running")
    drop = term.get("network_drop", "")
    if "reconnect" not in drop.lower() or "76" not in drop:
        bad("network_drop must state auto-reconnect attempts then exit 76")
    cold = term.get("cold_recovery", "")
    if "wrong_generation" not in cold or "NEW session" not in cold:
        bad("cold_recovery must show a NEW session notice and reference wrong_generation (#13)")

    # Telemetry: both fields, one local line, --quiet, target not promise.
    tele = doc["telemetry"]
    if set(tele.get("fields", [])) != REQUIRED_TELEMETRY_FIELDS:
        bad(f"telemetry.fields must be exactly {sorted(REQUIRED_TELEMETRY_FIELDS)}")
    if "one line" not in tele.get("output", ""):
        bad("telemetry.output must be one local line of output")
    if tele.get("quiet") != "--quiet suppresses the telemetry line":
        bad("telemetry.quiet must state --quiet suppresses the line")
    tnp = tele.get("target_not_promise", "")
    if "target" not in tnp or "not a promise" not in tnp:
        bad("telemetry must list 10s as a target, not a promise")

    # Install artifacts: version + checksum REQUIRED; claims only what is tested.
    art = doc["install_artifacts"]
    required = {a.get("artifact", ""): a.get("required") for a in art.get("release_requires", [])
                if isinstance(a, dict)}
    if not any("version" in k for k, v in required.items() if v is True):
        bad("release must require a version artifact")
    if not any("checksum" in k for k, v in required.items() if v is True):
        bad("release must require a checksum artifact")
    if "test" not in art.get("platform_claims", "").lower():
        bad("platform_claims must state support claims require real testing")
    for p in art.get("platforms", []):
        if isinstance(p, dict) and p.get("tested") is not False:
            bad(f"platform {p.get('platform')!r} may not claim support before real testing")

    # API gaps are never mocked.
    if "mocked success" not in doc["api_gaps"]:
        bad("api_gaps must state missing capabilities never answer with a mocked success")

    # Tickets: positive integers, required links, every #N string reference covered.
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
        bad(f"ticket #{n} must be linked")
    refs = {int(m) for m in TICKET_REF.findall(" ".join(iter_strings(doc)))}
    for n in sorted(refs - set(tickets)):
        bad(f"string reference #{n} has no integer entry in related_tickets")

    return violations


if __name__ == "__main__":
    source = Path(__file__).resolve().parents[1] / "docs/contracts/cli-surface.json"
    doc = json.loads(source.read_text())
    violations = check_contract(doc)
    for message in violations:
        print("FAIL", message, file=sys.stderr)
    if violations:
        sys.exit(1)
    print(f"{len(doc['commands'])} commands, {len(doc['unsupported_commands'])} unsupported, "
          f"{len(doc['exit_codes'])} exit codes, {len(doc['related_tickets'])} linked tickets: "
          f"consistent with issue #14, plan.md and the ADRs "
          f"(contract check only, no runtime verification claimed).")
