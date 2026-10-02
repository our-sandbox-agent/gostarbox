#!/usr/bin/env python3
"""Budget warning, stop and eligibility gates for #29 (stdlib only).

Executable RULES of the #29 acceptance (預算警示、停機與付費資格限制),
in-repo only — no metering loop, no runner, no Stripe, no DB, no clock.
Four pieces:

- EligibilityGate: the SINGLE eligibility source for create/resume/fork/
  resize reused by Web/CLI/API (共用 eligibility). Payment-state policy is
  DELEGATED to scripts/stripe_bridge.py BillingEligibility (#27 — aligned,
  not forked): canceled, payment-failed grace and unpaid-suspension (停權)
  rules all come from there. Budget denies (over-budget blocked) layer on
  top. admit() runs the eligibility check + budget increment as ONE
  critical section (the scripts/tenant_authz.py QuotaGate pattern) so N
  parallel creates cannot all pass a nearly-exhausted budget; admission
  also never commits a create whose result would REACH the limit
  (reserved headroom / early stop — the hard-cap requirement of the
  overrun model below).

- overrun_window(policy): the MAX possible overshoot between periodic
  (default 5-minute) checks = Σ(highest rate of active resources) ×
  check_interval + Σ(same rates) × stop_duration (accrual until the
  CONFIRMED stop — the #18 billing-cutoff rule). A hard cap requires BOTH
  reserved headroom ≥ max overshoot and early stop at limit − overshoot;
  the function reports both and flags an infeasible hard cap.

- BudgetEnforcer: thresholds → actions (warn at X%, block_new at Y%,
  persist-blocked-then-stop at limit). The AT-LIMIT sequence persists the
  `blocked` record FIRST (durable via injected storage), THEN issues stop
  (suspend) operations — and every individual stop call is preceded by its
  durable issued-mark, so a stop is never in flight without durable
  evidence. A stop timeout leaves the record at `blocked_stop_pending`
  with alerts and a watchdog follow-up reference (#18); an issued-but-
  unconfirmed stop is NEVER re-issued (the remote may have succeeded —
  the #27 outbox semantics); `blocked` persists across restart via the
  injected storage (no unblocked leak — only an explicit new_period or
  settlement clears it). Stops are SUSPENDS: confirm-before-billing-cutoff
  and never-auto-destroy are inherited from scripts/watchdog_lease.py
  (#18); this module never destroys anything.

- Storage economics: storage cost CONTINUES after compute is blocked
  (volumes accrue). BlockedStoragePolicy makes the product terms explicit
  — who pays storage during blocked, the cleanup timer, the accrual cap
  at cleanup — and new_period() implements 新帳期解除: the budget window
  resets but unpaid suspension survives (a new-period unlock is NOT debt
  forgiveness).

Honest state (狀態不假稱已零成本): compute accrues until every planned
stop is CONFIRMED (#18: the billing cutoff is the confirmed stop ONLY);
storage accrues for the whole blocked duration; the enforcer's cost
report never claims zero cost while anything accrues.

Thresholds (warn 80%, block-new 95%, 5-minute checks, 30s stop bound,
30-day blocked cleanup) are PROPOSED product defaults, not measured
constants — docs/contracts/budget-gates.md is the normative copy.

Tests: scripts/test_budget_gates.py.
Spec: docs/contracts/budget-gates.md.
"""
import copy
import dataclasses
import threading

from stripe_bridge import BillingEligibility, PAYMENT_STATES  # #27 — aligned, not forked

SCHEMA_VERSION = 1

# Web/CLI/API surface actions (issue #29: create/resume/fork/resize 共用
# eligibility). Mapping into the #27 BillingEligibility vocabulary: every
# action that starts or GROWS commitment counts as 'provision'; resume
# counts as 'resume'.
SURFACE_ACTIONS = ("create", "resume", "fork", "resize")
_ACTION_TO_BILLING = {"create": "provision", "fork": "provision",
                      "resize": "provision", "resume": "resume"}

# Deny vocabulary (單一來源: Web/CLI/API all surface these reasons)
REASON_CANCELED = "canceled"                      # 取消 (canceled/deleted: terminal)
REASON_UNPAID_SUSPENSION = "unpaid_suspension"    # 欠款停權 (grace expired)
REASON_PAYMENT_FAILED_GRACE = "payment_failed_grace"  # 寬限內封鎖新開通
REASON_NO_BILLING_EVIDENCE = "no_billing_evidence"    # open: trial only
REASON_OVER_BUDGET = "over_budget"                # 超額封鎖

DEFAULT_WARN_PCT = 80          # Proposed (product decision, not measured)
DEFAULT_BLOCK_NEW_PCT = 95     # Proposed (product decision, not measured)
DEFAULT_CHECK_INTERVAL_MS = 300_000   # 五分鐘檢查 (issue text)
DEFAULT_STOP_DURATION_MS = 30_000    # detect -> CONFIRMED stop bound (#18)
DEFAULT_CLEANUP_AFTER_MS = 30 * 24 * 3600 * 1000  # Proposed: 30 days


def _require_int(value, name, minimum=None):
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value!r}")


def _ceil_div(numerator, denominator):
    return -(-numerator // denominator)


def _require_pct(value, name):
    _require_int(value, name, minimum=1)
    if value >= 100:
        raise ValueError(f"{name} must be < 100, got {value!r}")
    return value


# --------------------------------------------------------------- eligibility
class EligibilityGate:
    """The single eligibility decision reused by Web, CLI and API.

    check(account_state, action) is a PURE function of the account state
    — one rule table, no surface-specific forks. Payment-state policy is
    delegated to BillingEligibility (#27); budget gating layers on top:

      budget_blocked (sticky persisted blocked state)      -> over_budget
      spend >= limit (at/over the limit)                   -> over_budget
      spend >= ceil(limit x block_new_pct) (block-new zone)-> over_budget

    admit(account_id, ...) runs the check plus the budget increment as
    ONE critical section (QuotaGate pattern; the optional stall hook
    widens the check->commit window purely so tests can observe what
    concurrency does). Admission additionally refuses a create whose
    result would REACH the limit — the reserved-headroom / early-stop
    half of the hard-cap requirement; the block-new threshold alone is
    the early-stop half.

    The named _-prefixed helpers are deliberate mutation points for the
    guard tests (scripts/test_budget_gates.py), not extension points.
    """

    def __init__(self, billing=None, block_new_pct=DEFAULT_BLOCK_NEW_PCT,
                 stall=None):
        self._billing = billing if billing is not None else BillingEligibility()
        self._block_new_pct = _require_pct(block_new_pct, "block_new_pct")
        self._lock = threading.Lock()
        self._stall = stall or (lambda: None)
        self._committed = {}   # account_id -> spend_minor admitted this period

    # ------------------------------------------------------------ decision
    def _block_new_at(self, limit_minor):
        """Early-block threshold; ceil so the percentage never blocks LATE
        (a floor could admit past the intended line)."""
        return _ceil_div(limit_minor * self._block_new_pct, 100)

    @staticmethod
    def _payment_denial(payment_state, decision):
        """Map a #27 billing block onto the #29 deny vocabulary (mutation
        point for the no-bypass guard)."""
        if payment_state in ("canceled", "deleted"):
            return REASON_CANCELED
        if payment_state == "payment_failed":
            return (REASON_UNPAID_SUSPENSION if decision["level"] == "blocked"
                    else REASON_PAYMENT_FAILED_GRACE)
        return REASON_NO_BILLING_EVIDENCE   # 'open': no billing evidence yet

    def check(self, account_state, action):
        """One decision for every surface (Web/CLI/API), pure and lock-free.

        account_state keys:
          payment_state  one of stripe_bridge.PAYMENT_STATES (absent ->
                        'open' — conservative: provision/resume denied)
          failed_at_ms / now_ms   grace anchor for payment_failed (#27)
          spend_minor / limit_minor     budget this period (limit None =
                        no budget cap configured)
          budget_blocked  sticky persisted blocked state (BudgetEnforcer)

        Returns {allowed, reason, action, payment_state, billing_level,
        budget} with reason None on allow, else one of the REASON_*
        vocabulary above.
        """
        if action not in SURFACE_ACTIONS:
            raise ValueError(f"action must be one of {SURFACE_ACTIONS}, got {action!r}")
        state = dict(account_state or {})
        payment_state = state.get("payment_state", "open")
        if payment_state not in PAYMENT_STATES:
            raise ValueError(f"payment_state must be one of {PAYMENT_STATES}, "
                             f"got {payment_state!r}")
        spend = state.get("spend_minor", 0)
        limit = state.get("limit_minor")
        _require_int(spend, "account_state.spend_minor", minimum=0)
        if limit is not None:
            _require_int(limit, "account_state.limit_minor", minimum=1)
        base = {"action": action, "payment_state": payment_state}
        billing = self._billing.evaluate(
            payment_state, failed_at_ms=state.get("failed_at_ms"),
            now_ms=state.get("now_ms", 0))
        base["billing_level"] = billing["level"]
        if _ACTION_TO_BILLING[action] in billing["blocked"]:
            return {**base, "allowed": False,
                    "reason": self._payment_denial(payment_state, billing),
                    "budget": None}
        # payment OK -> budget gate (over-budget blocked)
        if state.get("budget_blocked"):
            return {**base, "allowed": False, "reason": REASON_OVER_BUDGET,
                    "budget": {"detail": "blocked_state_sticky",
                               "spend_minor": spend, "limit_minor": limit}}
        if limit is not None:
            block_new_at = self._block_new_at(limit)
            if spend >= limit:
                return {**base, "allowed": False, "reason": REASON_OVER_BUDGET,
                        "budget": {"detail": "at_limit", "spend_minor": spend,
                                   "limit_minor": limit,
                                   "block_new_at_minor": block_new_at}}
            if spend >= block_new_at:
                return {**base, "allowed": False, "reason": REASON_OVER_BUDGET,
                        "budget": {"detail": "block_new_zone", "spend_minor": spend,
                                   "limit_minor": limit,
                                   "block_new_at_minor": block_new_at}}
        return {**base, "allowed": True, "reason": None,
                "budget": {"spend_minor": spend, "limit_minor": limit,
                           "block_new_at_minor": (None if limit is None
                                                  else self._block_new_at(limit))}}

    # ------------------------------------------------- atomic admission
    def admit(self, account_id, account_state, action, cost_minor):
        """Atomic eligibility check + budget increment (QuotaGate pattern).

        check-then-commit runs under one lock so N parallel creates cannot
        all pass a nearly-exhausted budget. The eligibility check reads
        the CURRENT ledger spend; a create whose result would reach the
        limit is refused (reserved headroom — the budget can never be
        admitted UP TO the limit; the last slice stays unreserved for the
        overrun window). Returns the check decision plus reserved_minor
        and the post-admission spend.
        """
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account_id must be a non-empty string")
        _require_int(cost_minor, "cost_minor", minimum=0)
        with self._lock:
            self._stall()
            if account_id not in self._committed:
                self._committed[account_id] = dict(account_state or {}).get(
                    "spend_minor", 0)
            base = self._committed[account_id]
            decision = self.check({**account_state, "spend_minor": base}, action)
            if not decision["allowed"]:
                return {**decision, "reserved_minor": 0, "spend_minor": base}
            limit = dict(account_state or {}).get("limit_minor")
            resulting = base + cost_minor
            if limit is not None and resulting >= limit:
                return {**decision, "allowed": False, "reason": REASON_OVER_BUDGET,
                        "budget": {"detail": "no_headroom_to_limit",
                                   "spend_minor": base, "limit_minor": limit},
                        "reserved_minor": 0, "spend_minor": base}
            if cost_minor:
                self._committed[account_id] = resulting
            return {**decision, "reserved_minor": cost_minor,
                    "spend_minor": self._committed[account_id]}

    def release(self, account_id, cost_minor=1):
        """Return reserved budget when a provisioning attempt failed."""
        _require_int(cost_minor, "cost_minor", minimum=0)
        with self._lock:
            self._stall()
            self._committed[account_id] = max(0, self._committed.get(account_id, 0)
                                              - cost_minor)

    def spend(self, account_id):
        return self._committed.get(account_id, 0)


# ------------------------------------------------------------------ overrun
@dataclasses.dataclass(frozen=True)
class ResourceRate:
    """One active resource's HIGHEST possible burn rate (the ceiling of its
    rate range, not the current rate — the max-overshoot model must bound
    the worst case)."""

    resource_id: str
    rate_minor_per_ms: int

    def __post_init__(self):
        if not isinstance(self.resource_id, str) or not self.resource_id:
            raise ValueError("resource_id must be a non-empty string")
        _require_int(self.rate_minor_per_ms, "rate_minor_per_ms", minimum=0)


@dataclasses.dataclass(frozen=True)
class OverrunPolicy:
    """Inputs of the max-overshoot model (issue: 五分鐘檢查最大超額)."""

    check_interval_ms: int = DEFAULT_CHECK_INTERVAL_MS
    stop_duration_ms: int = DEFAULT_STOP_DURATION_MS
    resources: tuple = ()     # tuple[ResourceRate, ...]

    def __post_init__(self):
        _require_int(self.check_interval_ms, "check_interval_ms", minimum=1)
        _require_int(self.stop_duration_ms, "stop_duration_ms", minimum=0)
        if isinstance(self.resources, (list, tuple)):
            resources = tuple(self.resources)
        else:
            raise ValueError("resources must be a sequence of ResourceRate")
        for item in resources:
            if not isinstance(item, ResourceRate):
                raise ValueError("resources must be ResourceRate instances")
        object.__setattr__(self, "resources", resources)


def overrun_window(policy, limit_minor=None):
    """Max possible overshoot between periodic checks (exact integer math).

    overrun = Σ(highest rate of active resources) x check_interval   (spend
              that can happen unnoticed between two checks)
            + Σ(same rates) x stop_duration                          (accrual
              until the CONFIRMED stop — #18: the billing cutoff is the
              confirmed stop ONLY, so stop time still bills)

    A HARD cap requires BOTH (the spec states the two together):
      reserved headroom >= max_overshoot below the limit, AND
      early stop at limit - max_overshoot (block_new before the wall).
    With limit_minor given, the function reports early_stop_at_minor and
    flags hard_cap_infeasible when the overshoot >= the limit (the burn
    rate / check interval / stop bound must shrink first — no threshold
    arithmetic can save it).
    """
    if not isinstance(policy, OverrunPolicy):
        raise ValueError("policy must be an OverrunPolicy")
    combined = sum(r.rate_minor_per_ms for r in policy.resources)
    detection = combined * policy.check_interval_ms
    stop_minor = combined * policy.stop_duration_ms
    overshoot = detection + stop_minor
    out = {
        "combined_rate_minor_per_ms": combined,
        "check_interval_ms": policy.check_interval_ms,
        "stop_duration_ms": policy.stop_duration_ms,
        "detection_minor": detection,
        "stop_minor": stop_minor,
        "max_overshoot_minor": overshoot,
        "hard_cap_headroom_minor": overshoot,
    }
    if limit_minor is not None:
        _require_int(limit_minor, "limit_minor", minimum=1)
        out["limit_minor"] = limit_minor
        out["early_stop_at_minor"] = limit_minor - overshoot
        out["hard_cap_infeasible"] = overshoot >= limit_minor
    return out


# ------------------------------------------------------- storage economics
@dataclasses.dataclass(frozen=True)
class BlockedStoragePolicy:
    """Storage-during-blocked terms (Proposed — product decision).

    Storage cost may CONTINUE after compute is blocked: volumes accrue.
    The policy names WHO pays during blocked, WHEN cleanup fires (and caps
    the accrual there — after cleanup the volumes are gone), and that a
    new period resets the BUDGET window only: 新帳期解除不能繞過欠款停權
    (new-period unlock != debt forgiveness).
    """

    storage_payer: str = "customer"          # Proposed: customer pays while blocked
    storage_rate_minor_per_ms: int = 0       # volume accrual rate while blocked
    cleanup_after_ms: int = DEFAULT_CLEANUP_AFTER_MS
    new_period_resets_budget: bool = True
    new_period_clears_unpaid_suspension: bool = False   # NEVER (issue text)

    def __post_init__(self):
        if not isinstance(self.storage_payer, str) or not self.storage_payer:
            raise ValueError("storage_payer must be a non-empty string")
        _require_int(self.storage_rate_minor_per_ms,
                     "storage_rate_minor_per_ms", minimum=0)
        _require_int(self.cleanup_after_ms, "cleanup_after_ms", minimum=1)

    def storage_accrued_minor(self, blocked_duration_ms):
        """Volumes accrue for the whole blocked duration, capped at the
        cleanup timer (cleanup ends the accrual by deleting the volumes).
        Exact integer math; a zero rate prices nothing but the accrual
        still REPORTS through this function, never silently elsewhere."""
        _require_int(blocked_duration_ms, "blocked_duration_ms", minimum=0)
        billable = min(blocked_duration_ms, self.cleanup_after_ms)
        return billable * self.storage_rate_minor_per_ms

    def cleanup_due(self, blocked_duration_ms):
        _require_int(blocked_duration_ms, "blocked_duration_ms", minimum=0)
        return blocked_duration_ms >= self.cleanup_after_ms


def new_period(account_state, policy=None):
    """新帳期解除: reset the BUDGET window only.

    spend_minor -> 0 and budget_blocked cleared (a fresh period re-arms
    the budget gates). payment_state is NEVER touched — an unpaid
    suspension (欠款停權) survives the new period; only settlement through
    the #27 payment path lifts it. This function is the ONLY budget-side
    unlock; there is no debt forgiveness anywhere in this module.
    """
    pol = policy if policy is not None else BlockedStoragePolicy()
    if not isinstance(pol, BlockedStoragePolicy):
        raise ValueError("policy must be a BlockedStoragePolicy")
    state = dict(account_state or {})
    if pol.new_period_resets_budget:
        state["spend_minor"] = 0
        state["budget_blocked"] = False
    state["period"] = dict(account_state or {}).get("period", 0) + 1
    return state


# ----------------------------------------------------------------- enforcer
LEVELS = ("ok", "warn", "block_new", "block_stop")


def _default_stop(resource_id):
    return {"resource_id": resource_id, "stopped": True}


class BudgetEnforcer:
    """Thresholds -> actions, plus the AT-LIMIT stop sequence.

    evaluate(spend_minor, limit_minor) is PURE: warn at warn_pct, block_new
    at block_new_pct, and at the limit (spend >= limit) the block_stop
    action set whose order is normative: persist the `blocked` record
    FIRST (durable, through the injected storage), THEN issue stop
    (suspend) operations. enforce() executes that sequence:

    - the blocked record persists before any stop attempt, and every stop
      CALL is preceded by its durable issued-mark — a stop is never in
      flight without durable evidence (crash windows converge);
    - an issued-but-unconfirmed stop is NEVER re-issued (the remote may
      have succeeded — the #27 outbox semantics): re-enforce/restart
      reports it as watchdog follow-up (#18) instead of double-stopping;
    - a stop timeout leaves the record at `blocked_stop_pending` with
      alerts (狀態不假稱已零成本: costs stay `still_accruing`);
    - the blocked state persists across restart via the injected storage —
      no unblocked leak; only new_period() or settlement clears it.

    Stops are SUSPENDS, never destroys (#18: never auto-destroy). The
    storage is called only under the enforcer lock, so a simple dict-like
    store suffices. The named _-prefixed helpers are deliberate mutation
    points for the guard tests, not extension points.
    """

    def __init__(self, storage, stop_op=None, warn_pct=DEFAULT_WARN_PCT,
                 block_new_pct=DEFAULT_BLOCK_NEW_PCT):
        if not callable(getattr(storage, "save", None)) or \
                not callable(getattr(storage, "load", None)):
            raise ValueError("storage must provide save(record) and load()")
        if stop_op is not None and not callable(stop_op):
            raise ValueError("stop_op must be callable")
        self._storage = storage
        self._stop_op = stop_op if stop_op is not None else _default_stop
        self._warn_pct = _require_pct(warn_pct, "warn_pct")
        self._block_new_pct = _require_pct(block_new_pct, "block_new_pct")
        if self._warn_pct >= self._block_new_pct:
            raise ValueError("warn_pct must be < block_new_pct")
        self._lock = threading.Lock()

    # ------------------------------------------------------------- pure
    def evaluate(self, spend_minor, limit_minor):
        """Pure threshold decision (mutation point for the at-limit guard):
        warn at X%, block_new at Y%, block_stop at spend >= limit."""
        _require_int(spend_minor, "spend_minor", minimum=0)
        _require_int(limit_minor, "limit_minor", minimum=1)
        warn_at = _ceil_div(limit_minor * self._warn_pct, 100)
        block_new_at = _ceil_div(limit_minor * self._block_new_pct, 100)
        thresholds = {"warn_at_minor": warn_at,
                      "block_new_at_minor": block_new_at,
                      "limit_minor": limit_minor}
        if spend_minor >= limit_minor:
            return {"level": "block_stop", "thresholds": thresholds,
                    "actions": ["persist_blocked_first", "issue_stops",
                                "watchdog_followup_on_stop_timeout"]}
        if spend_minor >= block_new_at:
            return {"level": "block_new", "thresholds": thresholds,
                    "actions": ["block_new_eligibility", "alert"]}
        if spend_minor >= warn_at:
            return {"level": "warn", "thresholds": thresholds,
                    "actions": ["alert"]}
        return {"level": "ok", "thresholds": thresholds, "actions": []}

    # ------------------------------------------------------- at-limit path
    def enforce(self, spend_minor, limit_minor, now_ms=0, resource_ids=()):
        """Run the judgment; at the limit, execute persist-then-stop."""
        judgment = self.evaluate(spend_minor, limit_minor)
        if judgment["level"] != "block_stop":
            return {"level": judgment["level"], "actions": judgment["actions"],
                    "thresholds": judgment["thresholds"], "phase": None,
                    "stop_calls": 0}
        _require_int(now_ms, "now_ms", minimum=0)
        if isinstance(resource_ids, str):
            raise ValueError("resource_ids must be a sequence, not a string")
        return self._at_limit(spend_minor, limit_minor, now_ms,
                              tuple(resource_ids))

    def _new_record(self, spend_minor, limit_minor, now_ms, planned):
        return {
            "schema_version": SCHEMA_VERSION,
            "phase": "blocked",          # -> blocked_stopped | blocked_stop_pending
            "blocked_at_ms": now_ms,
            "spend_minor": spend_minor,
            "limit_minor": limit_minor,
            "stops_planned": list(planned),
            "stops_issued": [],          # stop CALL initiated (durable mark)
            "stops_confirmed": [],       # stop receipt obtained
            "alerts": ["budget_blocked"],
        }

    def _at_limit(self, spend_minor, limit_minor, now_ms, planned):
        """PERSIST the blocked record FIRST, then issue stops (mutation
        point for the persist-then-stop ordering guard)."""
        with self._lock:
            record = self._storage.load()
            if record is None:
                record = self._new_record(spend_minor, limit_minor, now_ms,
                                          planned)
                self._storage.save(record)   # durable BEFORE any stop attempt
                fresh = True
            else:
                fresh = False
        if fresh:
            report = self._issue_stops(record)
            report["status"] = "blocked"
            return report
        return self._resume_blocked(record)

    def _resume_blocked(self, record):
        """Re-enforce / restart over an existing blocked record: blocked
        PERSISTS (no unblocked leak). Stops never issued (crash between
        persist and issue) are issued now; issued-but-unconfirmed are left
        to the watchdog (#18) — never re-issued."""
        report = self._issue_stops(record)
        report["status"] = "already_blocked"
        return report

    def _never_issued(self, record):
        """Re-issue filter (mutation point for the double-stop guard): a
        stop already ISSUED is never sent again — the remote may have
        succeeded (#27 semantics); the watchdog (#18) collects the
        confirmation."""
        return [rid for rid in record["stops_planned"]
                if rid not in record["stops_issued"]]

    def _set_phase(self, record):
        """Phase derivation: fully confirmed -> blocked_stopped; anything
        issued-but-unconfirmed keeps a pending/alerting phase (honest
        state)."""
        if set(record["stops_confirmed"]) >= set(record["stops_planned"]):
            record["phase"] = "blocked_stopped"
        # else keep the current phase: 'blocked' while the wave is in
        # flight, 'blocked_stop_pending' once a timeout was seen

    def _issue_stops(self, record):
        """Issue stop (suspend) for every planned resource never issued.

        Each step re-loads the AUTHORITATIVE record under the lock and
        re-checks the issue filter before marking (concurrent enforcers /
        a restart converge on exactly one call per resource — no double
        stop). The durable issued-mark is saved BEFORE the call; the stop
        call itself runs outside the lock (it may block)."""
        sent = 0
        for rid in self._never_issued(record):
            with self._lock:
                current = self._storage.load() or record
                if rid not in self._never_issued(current):
                    continue   # issued while we got here — never a second call
                current["stops_issued"].append(rid)
                self._storage.save(current)   # durable trace BEFORE the call
            sent += 1
            try:
                self._stop_op(rid)
            except TimeoutError:
                with self._lock:
                    current = self._storage.load() or record
                    current["phase"] = "blocked_stop_pending"
                    for alert in ("stop_timeout", "watchdog_followup"):
                        if alert not in current["alerts"]:
                            current["alerts"].append(alert)
                    self._storage.save(current)
                continue
            with self._lock:
                current = self._storage.load() or record
                current["stops_confirmed"].append(rid)
                self._set_phase(current)
                self._storage.save(current)
        with self._lock:
            current = self._storage.load() or record
            self._set_phase(current)
            self._storage.save(current)
            return self._blocked_report(current, sent)

    def _costs(self, record):
        """Honest cost status (mutation point for the honesty guard):
        compute accrues until EVERY planned stop is CONFIRMED (#18: the
        billing cutoff is the confirmed stop ONLY); storage accrues for
        the whole blocked duration (volumes — see BlockedStoragePolicy).
        NEVER claim zero cost while blocked (狀態不假稱已零成本)."""
        compute_stopped = (record["phase"] == "blocked_stopped")
        return {"compute_cost": "stopped" if compute_stopped else "still_accruing",
                "storage_cost": "still_accruing",
                "zero_cost": False}

    def _blocked_report(self, record, stop_calls):
        unconfirmed = [rid for rid in record["stops_issued"]
                       if rid not in record["stops_confirmed"]]
        return {
            "level": "block_stop",
            "status": "blocked",
            "phase": record["phase"],
            "blocked_at_ms": record["blocked_at_ms"],
            "stop_calls": stop_calls,
            "stops_confirmed": list(record["stops_confirmed"]),
            "watchdog_followup": unconfirmed,
            "alerts": list(record["alerts"]),
            "costs": self._costs(record),
            "record": copy.deepcopy(record),
        }
