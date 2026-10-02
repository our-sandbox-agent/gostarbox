"""Tests for the #29 budget gates (scripts/budget_gates.py).

Coverage per the issue acceptance (in-repo rules slice):
  - eligibility matrix: every payment state x every surface action
    (create/resume/fork/resize), over-budget denies, sticky blocked state
  - concurrency race: 30 barrier-synced threads vs a 1-slot budget — the
    atomic admit (QuotaGate pattern) admits exactly one; naive
    check-then-commit over-admits
  - overrun math exact (detection + stop terms, early stop, infeasible
    hard cap)
  - persist-then-stop ordering: the durable blocked record and each
    issued-mark precede every stop call (mutation guard: reorder fails)
  - in-flight stop race: budget at limit while a stop is in flight -> no
    double stop, no unblocked leak
  - blocked persists across restart via injected persistence
  - storage-continues accounting: volumes accrue while blocked, capped at
    the cleanup timer
  - new-period no-bypass: 新帳期解除 resets the budget window but never
    lifts unpaid suspension
  - stop-failure honesty: blocked_stop_pending keeps accruing costs,
    never claims zero cost (狀態不假稱已零成本)
  - mutation guards (CONTRIBUTING mutate-and-fail idiom): each guard
    breaks one safeguard at a deliberate point and asserts the invariant
    flips on the mutant and holds on the real implementation.
"""
import copy
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import budget_gates  # noqa: E402
from budget_gates import (  # noqa: E402
    BlockedStoragePolicy, BudgetEnforcer, EligibilityGate, OverrunPolicy,
    ResourceRate, SURFACE_ACTIONS, new_period, overrun_window)

DAY_MS = 24 * 3600 * 1000
GRACE_MS = 7 * DAY_MS   # stripe_bridge.BillingEligibility default


# ------------------------------------------------------------------ doubles
class DictStorage:
    """Injected durable store with ONE time-ordered timeline: every save
    and every stop call appends to it, so ordering checks see the true
    interleaving. save() stores a deep copy — saved records are frozen
    history."""

    def __init__(self):
        self.timeline = []      # ("save", frozen record) | ("stop", rid)
        self._record = None

    def save(self, record):
        frozen = copy.deepcopy(record)
        self.timeline.append(("save", frozen))
        self._record = frozen

    def load(self):
        return copy.deepcopy(self._record) if self._record is not None else None

    def note_stop(self, resource_id):
        self.timeline.append(("stop", resource_id))

    def saves(self):
        return [rec for kind, rec in self.timeline if kind == "save"]

    def stops(self):
        return [rid for kind, rid in self.timeline if kind == "stop"]


class StopOps:
    """Injected stop (suspend) operation with call counting."""

    def __init__(self, timeouts=(), before=None):
        self.timeouts = set(timeouts)
        self.before = before            # hook run inside the stop call
        self.calls = []

    def __call__(self, resource_id):
        self.calls.append(resource_id)
        if self.before is not None:
            self.before(resource_id)
        if resource_id in self.timeouts:
            raise TimeoutError(f"runner stop timed out for {resource_id}")
        return {"resource_id": resource_id, "stopped": True}


def logged_enforcer(timeouts=()):
    storage = DictStorage()
    ops = StopOps(timeouts=timeouts)
    ops.before = lambda rid: storage.note_stop(rid)
    return BudgetEnforcer(storage, stop_op=ops), storage, ops


def ordering_holds(storage):
    """Every stop call is preceded (in the timeline) by a durable save
    that already lists the resource in stops_issued."""
    stopped = 0
    for i, (kind, payload) in enumerate(storage.timeline):
        if kind != "stop":
            continue
        stopped += 1
        preceded = any(k == "save" and payload in rec["stops_issued"]
                       for k, rec in storage.timeline[:i])
        if not preceded:
            return False
    return stopped > 0


def paid(spend=0, limit=None, **extra):
    state = {"payment_state": "paid", "spend_minor": spend}
    if limit is not None:
        state["limit_minor"] = limit
    state.update(extra)
    return state


def run_concurrently(n, target):
    barrier = threading.Barrier(n)

    def runner():
        barrier.wait()
        target()

    threads = [threading.Thread(target=runner) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# --------------------------------------------------------------- eligibility
class TestEligibilityMatrix(unittest.TestCase):
    def setUp(self):
        self.gate = EligibilityGate()

    def test_paid_allows_all_surface_actions(self):
        for action in SURFACE_ACTIONS:
            decision = self.gate.check(paid(spend=0, limit=1000), action)
            self.assertTrue(decision["allowed"], action)
            self.assertIsNone(decision["reason"])

    def test_canceled_and_deleted_deny_everything(self):
        for state in ("canceled", "deleted"):
            for action in SURFACE_ACTIONS:
                decision = self.gate.check({"payment_state": state}, action)
                self.assertFalse(decision["allowed"], (state, action))
                self.assertEqual(decision["reason"], "canceled")

    def test_payment_failed_within_grace_blocks_new_allows_resume(self):
        state = {"payment_state": "payment_failed", "failed_at_ms": 1_000_000,
                 "now_ms": 1_000_000 + DAY_MS}
        for action in ("create", "fork", "resize"):
            decision = self.gate.check(state, action)
            self.assertFalse(decision["allowed"], action)
            self.assertEqual(decision["reason"], "payment_failed_grace")
        self.assertTrue(self.gate.check(state, "resume")["allowed"])

    def test_payment_failed_past_grace_is_unpaid_suspension_all_denied(self):
        state = {"payment_state": "payment_failed", "failed_at_ms": 1_000_000,
                 "now_ms": 1_000_000 + GRACE_MS}
        for action in SURFACE_ACTIONS:
            decision = self.gate.check(state, action)
            self.assertFalse(decision["allowed"], action)
            self.assertEqual(decision["reason"], "unpaid_suspension")

    def test_open_state_is_trial_only(self):
        for action in SURFACE_ACTIONS:
            decision = self.gate.check({"payment_state": "open"}, action)
            self.assertFalse(decision["allowed"], action)
            self.assertEqual(decision["reason"], "no_billing_evidence")
        # absent payment_state defaults conservative (open)
        self.assertEqual(self.gate.check({}, "create")["reason"],
                         "no_billing_evidence")

    def test_over_budget_at_limit_denied_even_when_paid(self):
        decision = self.gate.check(paid(spend=1000, limit=1000), "create")
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "over_budget")
        self.assertEqual(decision["budget"]["detail"], "at_limit")

    def test_over_budget_block_new_zone_denied(self):
        decision = self.gate.check(paid(spend=950, limit=1000), "fork")
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["budget"]["detail"], "block_new_zone")
        # below the threshold is allowed
        self.assertTrue(self.gate.check(paid(spend=949, limit=1000), "fork")["allowed"])

    def test_block_new_threshold_is_ceil_and_configurable(self):
        gate = EligibilityGate(block_new_pct=50)
        self.assertEqual(gate._block_new_at(1001), 501)  # ceil(500.5)
        self.assertTrue(gate.check(paid(spend=500, limit=1001), "create")["allowed"])
        self.assertFalse(gate.check(paid(spend=501, limit=1001), "create")["allowed"])

    def test_budget_blocked_flag_is_sticky_below_any_threshold(self):
        decision = self.gate.check(paid(spend=0, limit=10_000, budget_blocked=True),
                                   "resume")
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "over_budget")
        self.assertEqual(decision["budget"]["detail"], "blocked_state_sticky")

    def test_no_limit_configured_means_no_budget_deny(self):
        self.assertTrue(self.gate.check(paid(spend=10**9), "create")["allowed"])

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            self.gate.check(paid(), "destroy")          # not a surface action
        with self.assertRaises(ValueError):
            self.gate.check({"payment_state": "wealthy"}, "create")
        with self.assertRaises(ValueError):
            self.gate.check({"payment_state": "paid", "spend_minor": "12"},
                            "create")


# ------------------------------------------------------------- atomic admit
class TestAtomicAdmission(unittest.TestCase):
    def test_admit_reserves_and_denies_in_block_zone(self):
        gate = EligibilityGate()
        state = paid(spend=0, limit=1000)
        first = gate.admit("acct", state, "create", 100)
        self.assertTrue(first["allowed"])
        self.assertEqual(first["reserved_minor"], 100)
        self.assertEqual(first["spend_minor"], 100)
        # 100 + 850 = 950 enters the block-new zone for the NEXT create
        self.assertTrue(gate.admit("acct", state, "create", 850)["allowed"])
        denied = gate.admit("acct", state, "create", 1)
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["reason"], "over_budget")
        self.assertEqual(gate.spend("acct"), 950)

    def test_admit_never_commits_up_to_the_limit(self):
        gate = EligibilityGate()
        state = paid(spend=949, limit=1000)
        # one big create jumping the reserved slice straight to the limit
        wall = gate.admit("acct", state, "create", 51)    # 949 + 51 = 1000
        self.assertFalse(wall["allowed"])
        self.assertEqual(wall["budget"]["detail"], "no_headroom_to_limit")
        # staying under the limit commits
        ok = gate.admit("acct", state, "create", 48)      # 997 < 1000
        self.assertTrue(ok["allowed"])
        self.assertEqual(gate.spend("acct"), 997)
        # afterwards the block-new zone catches further creates first
        zone = gate.admit("acct", state, "create", 1)
        self.assertEqual(zone["budget"]["detail"], "block_new_zone")

    def test_release_returns_reserved_budget(self):
        gate = EligibilityGate()
        state = paid(spend=0, limit=1000)
        gate.admit("acct", state, "create", 100)
        gate.release("acct", 100)
        self.assertEqual(gate.spend("acct"), 0)
        gate.release("acct", 99)   # over-release floors at zero
        self.assertEqual(gate.spend("acct"), 0)

    def test_payment_denial_blocks_admit_without_touching_budget(self):
        gate = EligibilityGate()
        state = {"payment_state": "canceled", "spend_minor": 0, "limit_minor": 1000}
        denied = gate.admit("acct", state, "create", 10)
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["reason"], "canceled")
        self.assertEqual(denied["reserved_minor"], 0)
        self.assertEqual(gate.spend("acct"), 0)

    def test_threaded_race_1_slot_exactly_one_admitted(self):
        """30 barrier-synced threads vs a 1-slot budget (949/1000, one
        create of 1 fits before the 95% block-new line): the atomic admit
        lets EXACTLY ONE through; the naive count-then-commit closure
        over-admits. Removing the lock in admit makes the gate behave
        like the naive closure and FAILS this test."""
        threads, stall = 30, 0.002
        gate = EligibilityGate(stall=lambda: time.sleep(stall))
        state = paid(spend=949, limit=1000)
        allowed = []
        lock = threading.Lock()

        def gated_create():
            decision = gate.admit("acct", state, "create", 1)
            with lock:
                allowed.append(decision["allowed"])

        naive_used = {"spend": 949}
        naive_allowed = []

        def naive_create():
            spend = naive_used["spend"]        # count ...
            time.sleep(stall)                  # ... the race window ...
            if spend < 950:                    # ... blind commit over-admits
                naive_used["spend"] = spend + 1
                with lock:
                    naive_allowed.append(True)

        run_concurrently(threads, naive_create)
        run_concurrently(threads, gated_create)
        self.assertGreater(sum(naive_allowed), 1)      # the bug demonstrated
        self.assertEqual(sum(allowed), 1)              # bounded exactly
        self.assertEqual(gate.spend("acct"), 950)


# ------------------------------------------------------------------ overrun
class TestOverrunWindow(unittest.TestCase):
    def policy(self):
        return OverrunPolicy(
            check_interval_ms=300_000, stop_duration_ms=30_000,
            resources=(ResourceRate("sbx-a", 2), ResourceRate("sbx-b", 1)))

    def test_exact_math_detection_plus_stop_terms(self):
        window = overrun_window(self.policy())
        self.assertEqual(window["combined_rate_minor_per_ms"], 3)
        self.assertEqual(window["detection_minor"], 3 * 300_000)     # 900000
        self.assertEqual(window["stop_minor"], 3 * 30_000)           # 90000
        self.assertEqual(window["max_overshoot_minor"], 990_000)
        self.assertEqual(window["hard_cap_headroom_minor"], 990_000)

    def test_early_stop_and_infeasible_hard_cap(self):
        window = overrun_window(self.policy(), limit_minor=20_000_000)
        self.assertEqual(window["early_stop_at_minor"], 20_000_000 - 990_000)
        self.assertFalse(window["hard_cap_infeasible"])
        tiny = overrun_window(self.policy(), limit_minor=500_000)
        self.assertEqual(tiny["early_stop_at_minor"], 500_000 - 990_000)  # negative
        self.assertTrue(tiny["hard_cap_infeasible"])

    def test_no_active_resources_means_zero_overshoot(self):
        window = overrun_window(OverrunPolicy())
        self.assertEqual(window["max_overshoot_minor"], 0)

    def test_validation(self):
        with self.assertRaises(ValueError):
            ResourceRate("sbx", -1)
        with self.assertRaises(ValueError):
            ResourceRate("", 1)
        with self.assertRaises(ValueError):
            OverrunPolicy(check_interval_ms=0)
        with self.assertRaises(ValueError):
            OverrunPolicy(resources=("sbx-a",))   # not a ResourceRate
        with self.assertRaises(ValueError):
            overrun_window(self.policy(), limit_minor=0)


# ----------------------------------------------------------------- enforcer
class TestBudgetEnforcerLevels(unittest.TestCase):
    def setUp(self):
        self.enforcer, self.storage, self.ops = logged_enforcer()

    def test_threshold_boundaries_exact(self):
        ev = self.enforcer.evaluate
        self.assertEqual(ev(799, 1000)["level"], "ok")
        self.assertEqual(ev(800, 1000)["level"], "warn")      # warn at 80%
        self.assertEqual(ev(949, 1000)["level"], "warn")
        self.assertEqual(ev(950, 1000)["level"], "block_new")  # block at 95%
        self.assertEqual(ev(999, 1000)["level"], "block_new")
        self.assertEqual(ev(1000, 1000)["level"], "block_stop")  # AT the limit
        self.assertEqual(ev(1500, 1000)["level"], "block_stop")  # past it too

    def test_ceil_thresholds_and_custom_percentages(self):
        enforcer = BudgetEnforcer(DictStorage(), warn_pct=50, block_new_pct=51)
        self.assertEqual(enforcer.evaluate(49, 100)["level"], "ok")
        self.assertEqual(enforcer.evaluate(50, 100)["level"], "warn")
        self.assertEqual(enforcer.evaluate(51, 100)["level"], "block_new")
        with self.assertRaises(ValueError):
            BudgetEnforcer(DictStorage(), warn_pct=95, block_new_pct=80)
        with self.assertRaises(ValueError):
            self.enforcer.evaluate(0, 0)

    def test_below_limit_enforce_touches_no_storage(self):
        for spend in (0, 800, 950, 999):
            result = self.enforcer.enforce(spend, 1000, now_ms=1)
            self.assertIn(result["level"], ("ok", "warn", "block_new"))
            self.assertEqual(result["stop_calls"], 0)
        self.assertIsNone(self.storage.load())
        self.assertEqual(self.ops.calls, [])


class TestAtLimitSequence(unittest.TestCase):
    def test_persist_then_stop_order_and_report(self):
        enforcer, storage, ops = logged_enforcer()
        result = enforcer.enforce(1000, 1000, now_ms=5, resource_ids=("sbx-1",))
        self.assertEqual(result["level"], "block_stop")
        self.assertEqual(result["stop_calls"], 1)
        self.assertEqual(result["phase"], "blocked_stopped")
        self.assertEqual(result["stops_confirmed"], ["sbx-1"])
        self.assertEqual(result["watchdog_followup"], [])
        # the FIRST timeline event is the durable blocked record, saved
        # before any stop call
        kind, first = storage.timeline[0]
        self.assertEqual((kind, first["phase"]), ("save", "blocked"))
        self.assertEqual(first["stops_issued"], [])   # persist-first record
        self.assertEqual(storage.stops(), ["sbx-1"])
        self.assertEqual(storage.load()["phase"], "blocked_stopped")

    def test_every_stop_call_has_a_prior_durable_issued_mark(self):
        enforcer, storage, ops = logged_enforcer()
        enforcer.enforce(1000, 1000, now_ms=5, resource_ids=("a", "b"))
        self.assertTrue(ordering_holds(storage))
        self.assertEqual(storage.stops(), ["a", "b"])

    def test_in_flight_stop_race_no_double_stop(self):
        """Budget at limit while a stop is in flight: a concurrent enforce
        must not issue a second stop, and the blocked state is already
        durable mid-flight."""
        storage = DictStorage()
        started, release = threading.Event(), threading.Event()
        ops_calls = []
        ops_lock = threading.Lock()

        def slow_stop(rid):
            with ops_lock:
                ops_calls.append(rid)
            storage.note_stop(rid)
            started.set()
            self.assertTrue(release.wait(5))
            return {"resource_id": rid, "stopped": True}

        enforcer = BudgetEnforcer(storage, stop_op=slow_stop)
        results = {}

        def first_enforce():
            results["a"] = enforcer.enforce(1000, 1000, now_ms=1,
                                            resource_ids=("sbx-1",))

        t = threading.Thread(target=first_enforce)
        t.start()
        self.assertTrue(started.wait(5))          # stop in flight now
        durable = storage.load()                  # durable MID-flight
        self.assertEqual(durable["phase"], "blocked")
        self.assertEqual(durable["stops_issued"], ["sbx-1"])
        results["b"] = enforcer.enforce(1000, 1000, now_ms=2,
                                        resource_ids=("sbx-1",))
        self.assertEqual(results["b"]["status"], "already_blocked")
        self.assertEqual(results["b"]["stop_calls"], 0)
        release.set()
        t.join(5)
        self.assertEqual(results["a"]["phase"], "blocked_stopped")
        self.assertEqual(ops_calls, ["sbx-1"])    # exactly one stop call

    def test_blocked_persists_across_restart(self):
        enforcer, storage, ops = logged_enforcer()
        enforcer.enforce(1000, 1000, now_ms=5, resource_ids=("sbx-1",))
        # restart: a fresh enforcer over the SAME injected persistence
        ops2 = StopOps()
        enforcer2 = BudgetEnforcer(storage, stop_op=ops2)
        result = enforcer2.enforce(1200, 1000, now_ms=99, resource_ids=("sbx-1",))
        self.assertEqual(result["status"], "already_blocked")
        self.assertEqual(result["stop_calls"], 0)
        self.assertEqual(ops2.calls, [])          # no second stop
        self.assertEqual(result["phase"], "blocked_stopped")

    def test_stop_timeout_leaves_pending_and_watchdog_followup(self):
        enforcer, storage, ops = logged_enforcer(timeouts={"sbx-1"})
        result = enforcer.enforce(1000, 1000, now_ms=5, resource_ids=("sbx-1",))
        self.assertEqual(result["phase"], "blocked_stop_pending")
        self.assertIn("stop_timeout", result["alerts"])
        self.assertIn("watchdog_followup", result["alerts"])   # #18 follows up
        self.assertEqual(result["watchdog_followup"], ["sbx-1"])
        # never re-issue an issued-but-unconfirmed stop (remote may have won)
        again = enforcer.enforce(1100, 1000, now_ms=50, resource_ids=("sbx-1",))
        self.assertEqual(again["stop_calls"], 0)
        self.assertEqual(ops.calls, ["sbx-1"])
        self.assertEqual(again["phase"], "blocked_stop_pending")

    def test_crash_between_persist_and_issue_sends_on_restart(self):
        enforcer, storage, ops = logged_enforcer()
        # simulate the crash window: persist happened, no stop was issued
        record = enforcer._new_record(1000, 1000, 5, ("sbx-1",))
        storage.save(record)
        result = enforcer.enforce(1000, 1000, now_ms=6, resource_ids=("sbx-1",))
        self.assertEqual(result["stop_calls"], 1)     # never sent -> send now
        self.assertEqual(ops.calls, ["sbx-1"])
        self.assertEqual(result["phase"], "blocked_stopped")

    def test_empty_resource_ids_blocks_with_nothing_to_stop(self):
        enforcer, storage, ops = logged_enforcer()
        result = enforcer.enforce(1000, 1000, now_ms=5)
        self.assertEqual(result["phase"], "blocked_stopped")  # vacuously confirmed
        self.assertEqual(ops.calls, [])
        self.assertEqual(result["costs"]["compute_cost"], "stopped")

    def test_invalid_enforce_inputs(self):
        enforcer, _, _ = logged_enforcer()
        with self.assertRaises(ValueError):
            enforcer.enforce(1000, 1000, now_ms=-1)
        with self.assertRaises(ValueError):
            enforcer.enforce(1000, 1000, resource_ids="sbx-1")


# ------------------------------------------------- storage / new-period economics
class TestStorageEconomics(unittest.TestCase):
    def test_storage_continues_accruing_while_blocked(self):
        policy = BlockedStoragePolicy(storage_rate_minor_per_ms=1)
        enforcer, _, _ = logged_enforcer()
        result = enforcer.enforce(1000, 1000, now_ms=5, resource_ids=("sbx-1",))
        self.assertEqual(result["phase"], "blocked_stopped")   # compute stopped
        self.assertEqual(result["costs"]["storage_cost"], "still_accruing")
        self.assertGreater(policy.storage_accrued_minor(5000), 0)  # volumes accrue
        self.assertEqual(policy.storage_accrued_minor(5000), 5000)
        self.assertEqual(policy.storage_payer, "customer")     # Proposed term

    def test_accrual_caps_at_cleanup_timer(self):
        policy = BlockedStoragePolicy(storage_rate_minor_per_ms=1,
                                      cleanup_after_ms=1000)
        self.assertEqual(policy.storage_accrued_minor(999), 999)
        self.assertEqual(policy.storage_accrued_minor(1000), 1000)
        self.assertEqual(policy.storage_accrued_minor(50_000), 1000)  # capped
        self.assertFalse(policy.cleanup_due(999))
        self.assertTrue(policy.cleanup_due(1000))

    def test_new_period_resets_budget_but_not_unpaid_suspension(self):
        gate = EligibilityGate()
        unpaid = {"payment_state": "payment_failed", "failed_at_ms": 1_000,
                  "now_ms": 1_000 + GRACE_MS, "spend_minor": 1000,
                  "limit_minor": 1000, "budget_blocked": True}
        fresh = new_period(unpaid)
        self.assertEqual(fresh["spend_minor"], 0)          # budget re-armed
        self.assertFalse(fresh["budget_blocked"])
        self.assertEqual(fresh["payment_state"], "payment_failed")  # debt stays
        decision = gate.check({**fresh, "now_ms": 1_000 + GRACE_MS + DAY_MS},
                              "create")
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "unpaid_suspension")  # no bypass

    def test_new_period_unlocks_a_pure_budget_block(self):
        gate = EligibilityGate()
        blocked = paid(spend=1000, limit=1000, budget_blocked=True)
        self.assertEqual(gate.check(blocked, "create")["reason"], "over_budget")
        fresh = new_period(blocked)
        self.assertTrue(gate.check(fresh, "create")["allowed"])


# ------------------------------------------------------------------- guards
# Each mutant breaks one safeguard at a deliberate mutation point; each guard
# proves the invariant flips on the mutant and holds on the real rules
# (non-vacuity, CONTRIBUTING mutate-and-fail idiom).


class NoLockGate(EligibilityGate):
    def admit(self, account_id, account_state, action, cost_minor):
        # the lock removed: the forbidden check-then-commit split
        budget_gates._require_int(cost_minor, "cost_minor", minimum=0)
        if account_id not in self._committed:
            self._committed[account_id] = dict(account_state or {}).get(
                "spend_minor", 0)
        base = self._committed[account_id]
        self._stall()
        decision = self.check({**account_state, "spend_minor": base}, action)
        if not decision["allowed"]:
            return {**decision, "reserved_minor": 0, "spend_minor": base}
        self._committed[account_id] = base + cost_minor
        return {**decision, "reserved_minor": cost_minor,
                "spend_minor": base + cost_minor}


class StopBeforeMarkEnforcer(BudgetEnforcer):
    def _issue_stops(self, record):
        # the bug: stop CALL before the durable issued-mark (reordered)
        for rid in list(record["stops_planned"]):
            self._stop_op(rid)
            with self._lock:
                current = self._storage.load() or record
                current["stops_issued"].append(rid)
                current["stops_confirmed"].append(rid)
                current["phase"] = "blocked_stopped"
                self._storage.save(current)
        with self._lock:
            current = self._storage.load() or record
            return self._blocked_report(current, len(record["stops_planned"]))


class ReissuingEnforcer(BudgetEnforcer):
    def _never_issued(self, record):
        # the bug: forgets what was already issued — re-sends everything
        return list(record["stops_planned"])


class ZeroCostEnforcer(BudgetEnforcer):
    def _costs(self, record):
        # the bug: claims stopped/zero while stops are unconfirmed
        return {"compute_cost": "stopped", "storage_cost": "stopped",
                "zero_cost": True}


class LateBlockEnforcer(BudgetEnforcer):
    def evaluate(self, spend_minor, limit_minor):
        # the bug: off-by-one — exactly AT the limit no stop is proposed
        judgment = super().evaluate(spend_minor, limit_minor)
        if judgment["level"] == "block_stop" and spend_minor == limit_minor:
            return {"level": "block_new", "thresholds": judgment["thresholds"],
                    "actions": judgment["actions"]}
        return judgment


class FreeStoragePolicy(BlockedStoragePolicy):
    def storage_accrued_minor(self, blocked_duration_ms):
        return 0   # the bug: storage is free while blocked


class MutationGuards(unittest.TestCase):
    def test_guard_no_lock_over_admits_the_1_slot_budget(self):
        threads, stall = 30, 0.002
        state = paid(spend=949, limit=1000)

        def raced(gate):
            allowed = []
            lock = threading.Lock()

            def create():
                decision = gate.admit("acct", state, "create", 1)
                with lock:
                    allowed.append(decision["allowed"])

            run_concurrently(threads, create)
            return sum(allowed)

        real = EligibilityGate(stall=lambda: time.sleep(stall))
        self.assertEqual(raced(real), 1)
        broken = NoLockGate(stall=lambda: time.sleep(stall))
        self.assertGreater(raced(broken), 1)   # over-admits unlocked

    def test_guard_stop_before_mark_breaks_ordering(self):
        real_enforcer, real_storage, _ = logged_enforcer()
        real_enforcer.enforce(1000, 1000, now_ms=5, resource_ids=("sbx-1",))
        self.assertTrue(ordering_holds(real_storage))
        storage = DictStorage()
        ops = StopOps()
        ops.before = lambda rid: storage.note_stop(rid)
        broken = StopBeforeMarkEnforcer(storage, stop_op=ops)
        broken.enforce(1000, 1000, now_ms=5, resource_ids=("sbx-1",))
        self.assertFalse(ordering_holds(storage))   # stop outran its mark

    def test_guard_reissue_double_stops(self):
        real_enforcer, _, real_ops = logged_enforcer()
        real_enforcer.enforce(1000, 1000, now_ms=5, resource_ids=("sbx-1",))
        again = real_enforcer.enforce(1000, 1000, now_ms=6, resource_ids=("sbx-1",))
        self.assertEqual(again["stop_calls"], 0)
        self.assertEqual(real_ops.calls, ["sbx-1"])
        storage = DictStorage()
        ops = StopOps()
        broken = ReissuingEnforcer(storage, stop_op=ops)
        broken.enforce(1000, 1000, now_ms=5, resource_ids=("sbx-1",))
        broken.enforce(1000, 1000, now_ms=6, resource_ids=("sbx-1",))
        self.assertEqual(ops.calls, ["sbx-1", "sbx-1"])   # the double stop

    def test_guard_zero_cost_claim_breaks_honesty(self):
        def honest(enforcer):
            result = enforcer.enforce(1000, 1000, now_ms=5,
                                      resource_ids=("sbx-1",))
            return result["costs"]

        real_storage, real_ops = DictStorage(), StopOps(timeouts={"sbx-1"})
        real = BudgetEnforcer(real_storage, stop_op=real_ops)
        costs = honest(real)
        self.assertEqual(costs["compute_cost"], "still_accruing")
        self.assertEqual(costs["storage_cost"], "still_accruing")
        self.assertFalse(costs["zero_cost"])
        broken_storage, broken_ops = DictStorage(), StopOps(timeouts={"sbx-1"})
        broken = ZeroCostEnforcer(broken_storage, stop_op=broken_ops)
        self.assertEqual(honest(broken)["compute_cost"], "stopped")  # the lie

    def test_guard_new_period_debt_forgiveness_bypasses_suspension(self):
        gate = EligibilityGate()
        unpaid = {"payment_state": "payment_failed", "failed_at_ms": 1_000,
                  "now_ms": 1_000 + GRACE_MS + DAY_MS}

        def first_deny(state):
            return gate.check(new_period(state), "create")["reason"]

        self.assertEqual(first_deny(unpaid), "unpaid_suspension")   # holds

        def forgiving_new_period(account_state, policy=None):
            state = new_period(account_state, policy)
            state["payment_state"] = "paid"   # the bug: debt forgiveness
            return state

        decision = gate.check(forgiving_new_period(unpaid), "create")
        self.assertTrue(decision["allowed"])                       # flipped

    def test_guard_free_storage_breaks_accrual(self):
        real = BlockedStoragePolicy(storage_rate_minor_per_ms=1)
        self.assertEqual(real.storage_accrued_minor(10_000), 10_000)
        broken = FreeStoragePolicy(storage_rate_minor_per_ms=1)
        self.assertEqual(broken.storage_accrued_minor(10_000), 0)  # free

    def test_guard_at_limit_off_by_one_loses_the_stop(self):
        real = BudgetEnforcer(DictStorage())
        self.assertEqual(real.evaluate(1000, 1000)["level"], "block_stop")
        self.assertIsNotNone(real.enforce(1000, 1000, now_ms=1)["phase"])
        broken = LateBlockEnforcer(DictStorage())
        self.assertEqual(broken.evaluate(1000, 1000)["level"], "block_new")
        self.assertIsNone(broken.enforce(1000, 1000, now_ms=1)["phase"])

    def test_guard_overrun_without_stop_term_understates(self):
        policy = OverrunPolicy(
            check_interval_ms=300_000, stop_duration_ms=30_000,
            resources=(ResourceRate("sbx-a", 2), ResourceRate("sbx-b", 1)))

        def without_stop_term(pol, limit_minor=None):
            window = dict(overrun_window(pol, limit_minor))
            window["max_overshoot_minor"] = window["detection_minor"]  # term dropped
            return window

        real = overrun_window(policy)
        broken = without_stop_term(policy)
        self.assertEqual(real["max_overshoot_minor"], 990_000)
        self.assertEqual(broken["max_overshoot_minor"], 900_000)  # understated
        self.assertNotEqual(real["max_overshoot_minor"],
                            broken["max_overshoot_minor"])


if __name__ == "__main__":
    unittest.main()
