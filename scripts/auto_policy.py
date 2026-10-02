#!/usr/bin/env python3
"""Conservative automatic Idle/Suspend PRODUCT policy for #19 (stdlib only).

Pure SERVER-SIDE state machine `decide(state, signals, policy)` plus the
display helper `countdown(policy, now, last_signal)`. This is the product
policy required by #19 and is deliberately DISTINCT from the browser DEMO
clock in lifecycle.js (the GitHub Pages demo excludes closed-page time;
here the server evaluates the decision from persisted timestamps, so
closing the browser changes nothing — policy／countdown 由伺服器決定).

Signals are SEPARATE and follow the terminal-protocol activity classes
(docs/contracts/terminal-protocol.json): user_input (terminal `input`
frames and, for the future IDE mode #52, IDE WebSocket client→server
messages) is the PRIMARY idle signal; ping/keepalive (liveness class),
resize (layout class) and output (server_stream class) are NEVER
activity; cpu_demand and new_task are WAKEUPS that must not be ignored
even though user input is primary (2026-10-02 issue note; ADR
sandbox-lifecycle.md section 4).

Conservative-when-uncertain is the default posture: unknown observed
state or clock, a missing activity anchor, an unrecognized signal value,
or an expired/malformed busy lease each keeps the CURRENT state instead
of demoting. Silence is never completion: a missing busy hook or an
expired busy lease becomes busy_status `unknown_busy` + alert +
requires_human (the acceptance 轉 unknown／告警 rule), NEVER done — so a
2h+ low-CPU task under a valid, renewed busy lease survives both
Active→Idle and Idle→Suspend.

Error/Lost — and every observed_state outside {Active, Idle} — are
IMMUNE: timers never override them (2026-10-02 note), and a pending
operation (suspend/resume/destroy in flight) blocks every new auto
transition (one change operation per sandbox, #17). The recovery
workflow itself lives in #17 and is NOT duplicated here; quota reach /
watchdog kill is #18 (the explicit stop path is runtime_deadline_at,
never this idle machine).

The interface takes an exclusions list (policy `excluded_processes`,
e.g. code-server): CPU demand attributed to an excluded resident process
is not a wakeup — otherwise an IDE heartbeat would keep the sandbox from
ever demoting (2026-09-22 founder note). This ticket is terminal-first;
the list is the reserved IDE interface.

No thresholds are snuck in: a None/absent threshold DISABLES that auto
transition entirely (ADR sandbox-lifecycle.md section 4 — 自動門檻數值
在 #19 以工作負載測試確定，未確定前不偷填常數). decide() only PROPOSES
actions for the caller to run through the existing operation path
(docs/contracts/runner-lifecycle.json); it mutates nothing and reads no
clock — all time arrives via signals["now"].

Tests: scripts/test_auto_policy.py.
Spec: docs/contracts/auto-policy.md.
"""

SCHEMA_VERSION = 1

# docs/contracts/runner-lifecycle.json states
STATES = frozenset({
    "Creating", "Active", "Idle", "Suspending", "Suspend", "Resuming",
    "Destroying", "Destroyed", "Lost", "Error",
})
# Only Active/Idle are auto-policed. Everything else — Error, Lost and
# all intermediate states — gets NO auto transition: timers do not
# override them (2026-10-02 note).
AUTO_MANAGED = frozenset({"Active", "Idle"})

# busy classification outcomes
BUSY_NONE = "none"
BUSY_PROTECTED = "protected"     # valid lease, done=False: demotion FORBIDDEN
BUSY_COMPLETE = "complete"       # verified matching completion: no protection
BUSY_UNKNOWN = "unknown_busy"    # hook 遺失／過期／malformed: KEEP + alert

_WAKE_KEYS = ("user_input", "new_task")
# Signals accepted and deliberately NEVER consulted as activity
# (terminal-protocol activity classes: liveness, layout, server_stream):
_NOT_ACTIVITY_KEYS = ("ping", "resize", "output")


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ------------------------------------------------------------- policy
def _threshold(value, name):
    """None disables the transition (no constant is invented); a positive
    number enables it; anything else is a misconfiguration, fail loudly."""
    if value is None:
        return None
    if not _is_num(value) or value <= 0:
        raise ValueError(
            f"{name} must be a positive number or None, got {value!r}")
    return value


def _flag(value, name):
    if value is None:
        return True
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean or None, got {value!r}")
    return value


def _non_negative(value, name, default):
    if value is None:
        return default
    if not _is_num(value) or value < 0:
        raise ValueError(
            f"{name} must be a non-negative number or None, got {value!r}")
    return value


def normalize_policy(policy=None):
    """Validate/fill the workspace policy shape (ADR section 4 fields).
    Invalid values raise ValueError; absent thresholds return None
    (= transition disabled, the conservative default)."""
    p = dict(policy or {})
    excluded = p.get("excluded_processes")
    if excluded is None:
        excluded = ()
    if isinstance(excluded, str) or not all(
            isinstance(n, str) and n for n in excluded):
        raise ValueError(
            "excluded_processes must be an iterable of non-empty names")
    return {
        "auto_idle_enabled": _flag(p.get("auto_idle_enabled"),
                                   "auto_idle_enabled"),
        "idle_after_seconds": _threshold(p.get("idle_after_seconds"),
                                         "idle_after_seconds"),
        "auto_suspend_enabled": _flag(p.get("auto_suspend_enabled"),
                                      "auto_suspend_enabled"),
        "suspend_after_seconds": _threshold(p.get("suspend_after_seconds"),
                                            "suspend_after_seconds"),
        "cpu_demand_threshold_milli": _non_negative(
            p.get("cpu_demand_threshold_milli"),
            "cpu_demand_threshold_milli", 0),
        "demotion_cooldown_seconds": _non_negative(
            p.get("demotion_cooldown_seconds"),
            "demotion_cooldown_seconds", 0),
        "excluded_processes": frozenset(excluded),
    }


# ------------------------------------------------------------- signals
def _cpu_excluded(source, policy):
    """CPU demand attributed to an excluded resident process (code-server)
    is not a wakeup — the reserved IDE exclusion interface."""
    return isinstance(source, str) and source in policy["excluded_processes"]


def _signal_wakeup(signals, policy):
    """(wake_reason | None, uncertain: bool) over the SEPARATE classes.

    user_input/new_task count only as strict booleans; cpu_demand_milli
    counts when numeric, positive and >= the threshold, unless its
    process is excluded. ping/resize/output are never read (_NOT_ACTIVITY_
    KEYS). A present-but-unrecognized value is `uncertain`: it blocks
    DEMOTION (conservative-when-uncertain) but never blocks a recognized
    wake."""
    wake_reason, uncertain = None, False
    for key in _WAKE_KEYS:
        value = signals.get(key)
        if value is None:
            continue
        if value is True:
            wake_reason = wake_reason or key
        else:
            uncertain = True
    cpu = signals.get("cpu_demand_milli")
    if cpu is not None:
        if not _is_num(cpu):
            uncertain = True
        elif (cpu > 0 and cpu >= policy["cpu_demand_threshold_milli"]
                and not _cpu_excluded(signals.get("cpu_demand_process"),
                                      policy)):
            wake_reason = wake_reason or "cpu_demand"
    return wake_reason, uncertain


# ---------------------------------------------------------------- busy
def _admissible_generation(report, state):
    """A report from another generation is history only (the #17 fencing
    convention): it cannot lift — or set — current busy state."""
    report_gen = report.get("generation")
    state_gen = state.get("generation")
    return report_gen is None or state_gen is None or report_gen == state_gen


def _classify_busy_snapshot(snapshot, now):
    """One busy snapshot (fresh hook report or persisted claim) →
    (status, alerts). Expiry is checked BEFORE done: 過期不視為任務完成."""
    if not isinstance(snapshot, dict):
        return BUSY_UNKNOWN, ["unknown_busy_report"]
    if not snapshot.get("work_id"):
        return BUSY_UNKNOWN, ["unknown_busy_report"]
    lease = snapshot.get("lease_expires_at")
    if not _is_num(lease):
        return BUSY_UNKNOWN, ["unknown_busy_report"]
    if now >= lease:                       # half-open: expired once now >= lease
        return BUSY_UNKNOWN, ["busy_lease_expired"]
    if snapshot.get("done") is True:
        return BUSY_COMPLETE, []
    return BUSY_PROTECTED, []


def _busy_status(state, signals, now):
    """(status, alerts, requires_human) merging the persisted claim with
    the fresh hook report.

    Renewal (續租) is a fresh report extending lease_expires_at; when
    reports stop arriving the persisted claim's lease eventually expires
    → unknown_busy, never done. A completion report only lifts protection
    when it matches the claimed work_id and the current generation —
    晚到的舊完成訊號不能蓋掉新 busy (ADR section 4)."""
    if signals.get("busy_hook_installed") is False:
        return BUSY_UNKNOWN, ["busy_hook_missing"], True   # hook 遺失
    report, claim = signals.get("busy"), state.get("busy")
    dropped_stale = False
    if report is not None and not _admissible_generation(report, state):
        report = None                        # history only, never current
        dropped_stale = True
    if report is not None:
        status, alerts = _classify_busy_snapshot(report, now)
        if (status == BUSY_COMPLETE and isinstance(claim, dict)
                and claim.get("work_id")
                and report.get("work_id") != claim.get("work_id")):
            # completion for a DIFFERENT work id: the claimed work stays
            status, alerts = _classify_busy_snapshot(claim, now)
            alerts = ["late_completion_ignored"] + alerts
        if dropped_stale:
            alerts = ["stale_busy_report_ignored"] + alerts
        return status, alerts, status == BUSY_UNKNOWN
    if claim is not None:
        status, alerts = _classify_busy_snapshot(claim, now)
        if dropped_stale:
            alerts = ["stale_busy_report_ignored"] + alerts
        return status, alerts, status == BUSY_UNKNOWN
    if dropped_stale:
        return BUSY_NONE, ["stale_busy_report_ignored"], False
    return BUSY_NONE, [], False


# ------------------------------------------------------------ demotion
def _activity_anchor(state, signals, wake_reason):
    """Latest timestamp anchoring the demotion countdown; None when
    unknown (→ conservative hold). A current wake signal anchors at now.
    last_demotion_at deliberately does NOT anchor anything."""
    candidates = [state.get("last_user_input_at"),
                  state.get("last_wakeup_at"),
                  state.get("state_since")]
    if wake_reason is not None:
        now = signals.get("now")
        if _is_num(now):
            candidates.append(now)
    values = [v for v in candidates if _is_num(v)]
    return max(values) if values else None


def _cooldown_blocks(state, now, policy):
    """Cooldown BETWEEN demotions: after an auto demotion recorded in
    state.last_demotion_at, the next demotion waits cooldown seconds.
    It gates demotion ONLY — a manual input wake is never blocked
    (decide checks wakes before this gate)."""
    cooldown = policy["demotion_cooldown_seconds"]
    if not cooldown:
        return False
    last = state.get("last_demotion_at")
    return _is_num(last) and now - last < cooldown


def _pending_operation(state):
    return bool(state.get("pending_operation"))


def _auto_managed(observed_state):
    return observed_state in AUTO_MANAGED


# ------------------------------------------------------------- decide
def decide(state, signals, policy):
    """Pure decision for one sandbox at signals["now"].

    Returns {action, observed_state, reason, alerts, busy_status,
    requires_human, now} where action is one of:
      hold          — no proposal (steady, guarded, or conservative)
      wake_active   — propose Idle→Active (user_input/cpu/new_task)
      request_idle  — propose Active→Idle (idle timer elapsed)
      request_suspend — propose Idle→Suspending via the suspend operation
    The caller drives any proposal through the #17 operation path; this
    function performs no transition itself.
    """
    pol = normalize_policy(policy)
    sig = dict(signals or {})
    st = dict(state or {})
    now = sig.get("now")
    observed = st.get("observed_state")

    def hold(reason, alerts=(), busy_status=BUSY_NONE, requires_human=False):
        return {"action": "hold", "observed_state": observed,
                "reason": reason, "alerts": list(alerts),
                "busy_status": busy_status, "requires_human": requires_human,
                "now": now if _is_num(now) else None}

    if not _is_num(now):
        return hold("unknown_clock", ["policy_unknown_clock"])
    if observed not in STATES:
        return hold("unknown_state", ["policy_unknown_state"])
    if _pending_operation(st):
        # 恢復 operation 進行中：timers/wakeups 不得蓋掉 (2026-10-02)
        return hold("operation_in_flight")
    if not _auto_managed(observed):
        # Error/Lost and every non-managed state: NO auto transitions
        return hold("no_auto_transitions")

    busy_status, busy_alerts, busy_human = _busy_status(st, sig, now)
    wake_reason, uncertain = _signal_wakeup(sig, pol)

    def propose_demotion(action, timer_reason):
        if uncertain:
            return hold("uncertain_signal", busy_alerts, busy_status,
                        busy_human)
        if busy_status == BUSY_PROTECTED:
            return hold("busy_protected", busy_alerts, busy_status,
                        busy_human)
        if busy_status == BUSY_UNKNOWN:
            return hold(BUSY_UNKNOWN, busy_alerts, busy_status, True)
        if _cooldown_blocks(st, now, pol):
            return hold("demotion_cooldown", busy_alerts, busy_status,
                        busy_human)
        return {"action": action, "observed_state": observed,
                "reason": timer_reason, "alerts": list(busy_alerts),
                "busy_status": busy_status, "requires_human": busy_human,
                "now": now}

    if wake_reason is not None and observed == "Idle":
        # ANY user_input/cpu_demand/new_task wakes Idle→Active immediately;
        # cooldown, busy and uncertainty never block a wake (manual input
        # included).
        return {"action": "wake_active", "observed_state": observed,
                "reason": wake_reason, "alerts": list(busy_alerts),
                "busy_status": busy_status, "requires_human": False,
                "now": now}

    if observed == "Active":
        if not (pol["auto_idle_enabled"]
                and pol["idle_after_seconds"] is not None):
            return hold("auto_idle_disabled", busy_alerts, busy_status,
                        busy_human)
        anchor = _activity_anchor(st, sig, wake_reason)
        if anchor is None:
            return hold("unknown_activity_anchor",
                        ["policy_unknown_anchor"] + busy_alerts,
                        busy_status, busy_human)
        if now - anchor < pol["idle_after_seconds"]:
            return hold(wake_reason or "steady", busy_alerts, busy_status,
                        busy_human)
        return propose_demotion("request_idle", "idle_timer")

    # observed == "Idle"
    if not (pol["auto_suspend_enabled"]
            and pol["suspend_after_seconds"] is not None):
        return hold("auto_suspend_disabled", busy_alerts, busy_status,
                    busy_human)
    anchor = _activity_anchor(st, sig, wake_reason=None)  # wakes returned above
    if anchor is None:
        return hold("unknown_activity_anchor",
                    ["policy_unknown_anchor"] + busy_alerts,
                    busy_status, busy_human)
    if now - anchor < pol["suspend_after_seconds"]:
        return hold(wake_reason or "steady", busy_alerts, busy_status,
                    busy_human)
    return propose_demotion("request_suspend", "suspend_timer")


# ----------------------------------------------------------- countdown
def countdown(policy, now, last_signal):
    """Server-computed seconds-to-next-demotion for display; the browser
    never computes policy (a closed browser changes nothing). Both
    countdowns anchor at the same last-signal timestamp and the caller
    picks the field matching observed_state. None when the transition is
    disabled or the inputs are unknown (無倒數為 null, ADR section 3)."""
    pol = normalize_policy(policy)
    out = {"idle_in": None, "suspend_in": None}
    if not _is_num(now) or not _is_num(last_signal):
        return out
    elapsed = max(0, now - last_signal)   # backward clock shows the full window
    if pol["auto_idle_enabled"] and pol["idle_after_seconds"] is not None:
        out["idle_in"] = max(0, pol["idle_after_seconds"] - elapsed)
    if pol["auto_suspend_enabled"] and pol["suspend_after_seconds"] is not None:
        out["suspend_in"] = max(0, pol["suspend_after_seconds"] - elapsed)
    return out
