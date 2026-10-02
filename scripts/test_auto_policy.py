"""Table-driven guard tests for the #19 auto idle/suspend policy slice.

Covers the issue acceptance without a runtime: the full SEPARATE signal
matrix (input, output, resize, ping/keepalive, CPU demand, busy/done as
distinct signals), 2h+ low-CPU busy survival on a simulated long
timeline, busy-lease expiry → unknown_busy with NO demotion and never
done, hook missing → same, Error/Lost/pending-operation immunity to
timers, cooldown between demotions vs never-blocked manual input wake,
ping/resize/output not being activity, cpu_demand/new_task wakeups, the
IDE exclusion interface, server-side countdown, conservative-when-
uncertain defaults, and mutation guards proving each safeguard is
load-bearing (pattern per scripts/test_watchdog_lease.py).
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import auto_policy as ap  # noqa: E402
from auto_policy import countdown, decide, normalize_policy  # noqa: E402

HOUR = 3600
BASE_POLICY = {
    "idle_after_seconds": 60,
    "suspend_after_seconds": 300,
    "cpu_demand_threshold_milli": 250,
    "demotion_cooldown_seconds": 120,
    "excluded_processes": ("code-server",),
}


def st(**over):
    """Sandbox state at now=1000 with an elapsed timer by default."""
    state = {"observed_state": "Active", "state_since": 0,
             "last_user_input_at": 0, "last_wakeup_at": None,
             "last_demotion_at": None, "busy": None, "generation": 1,
             "pending_operation": None}
    state.update(over)
    return state


def sg(**over):
    signals = {"now": 1000, "user_input": None, "output": None,
               "resize": None, "ping": None, "cpu_demand_milli": None,
               "cpu_demand_process": None, "new_task": None,
               "busy": None, "busy_hook_installed": None}
    signals.update(over)
    return signals


def busy(lease=1200, done=False, work_id="w1", generation=None):
    report = {"work_id": work_id, "lease_expires_at": lease, "done": done}
    if generation is not None:
        report["generation"] = generation
    return report


# ----------------------------------------------------------- the table
# (name, state, signals, policy_overrides, expected fields)
CASES = [
    # ---- Active: separate signal classes
    ("active within window holds", st(last_user_input_at=990), sg(), {},
     {"action": "hold", "reason": "steady"}),
    ("active idle timer demotes", st(last_user_input_at=0), sg(), {},
     {"action": "request_idle", "reason": "idle_timer"}),
    ("boundary exactly idle_after demotes", st(last_user_input_at=940),
     sg(), {}, {"action": "request_idle"}),
    ("just under boundary holds", st(last_user_input_at=941), sg(), {},
     {"action": "hold", "reason": "steady"}),
    ("current user_input refreshes the countdown", st(last_user_input_at=0),
     sg(user_input=True), {}, {"action": "hold", "reason": "user_input"}),
    ("ping is not activity", st(last_user_input_at=0), sg(ping=True), {},
     {"action": "request_idle"}),
    ("resize is not activity", st(last_user_input_at=0), sg(resize=True),
     {}, {"action": "request_idle"}),
    ("output is not activity", st(last_user_input_at=0), sg(output=True),
     {}, {"action": "request_idle"}),
    ("cpu below threshold is not a wakeup", st(last_user_input_at=0),
     sg(cpu_demand_milli=100), {}, {"action": "request_idle"}),
    ("cpu demand wakes Active", st(last_user_input_at=0),
     sg(cpu_demand_milli=300), {}, {"action": "hold",
                                     "reason": "cpu_demand"}),
    ("new_task wakes Active", st(last_user_input_at=0), sg(new_task=True),
     {}, {"action": "hold", "reason": "new_task"}),
    ("excluded code-server cpu is not a wakeup", st(last_user_input_at=0),
     sg(cpu_demand_milli=900, cpu_demand_process="code-server"), {},
     {"action": "request_idle"}),
    ("non-excluded process cpu wakes", st(last_user_input_at=0),
     sg(cpu_demand_milli=300, cpu_demand_process="node"), {},
     {"action": "hold", "reason": "cpu_demand"}),
    ("cpu zero is not a wakeup", st(last_user_input_at=0),
     sg(cpu_demand_milli=0), {}, {"action": "request_idle"}),
    ("cpu None is not a wakeup", st(last_user_input_at=0),
     sg(cpu_demand_milli=None), {}, {"action": "request_idle"}),
    # ---- busy protection on Active
    ("valid busy claim forbids demotion", st(last_user_input_at=0,
                                             busy=busy()),
     sg(), {}, {"action": "hold", "reason": "busy_protected",
                "busy_status": "protected"}),
    ("fresh busy report forbids demotion", st(last_user_input_at=0),
     sg(busy=busy()), {}, {"action": "hold", "reason": "busy_protected"}),
    ("done matching work_id lifts protection", st(last_user_input_at=0,
                                                   busy=busy(work_id="w1")),
     sg(busy=busy(done=True, work_id="w1")), {},
     {"action": "request_idle", "busy_status": "complete"}),
    ("late done for another work_id ignored",
     st(last_user_input_at=0, generation=2,
        busy=busy(work_id="w2", generation=2)),
     sg(busy=busy(done=True, work_id="w1", generation=2)), {},
     {"action": "hold", "reason": "busy_protected",
      "alerts_in": ["late_completion_ignored"]}),
    ("expired busy lease is unknown_busy, not done",
     st(last_user_input_at=0, busy=busy(lease=999, done=False)), sg(), {},
     {"action": "hold", "reason": "unknown_busy",
      "busy_status": "unknown_busy", "alerts_in": ["busy_lease_expired"],
      "requires_human": True}),
    ("done with expired lease is still unknown_busy",
     st(last_user_input_at=0), sg(busy=busy(lease=999, done=True)), {},
     {"action": "hold", "reason": "unknown_busy",
      "alerts_in": ["busy_lease_expired"]}),
    ("malformed busy report (no work_id) is unknown_busy",
     st(last_user_input_at=0),
     sg(busy={"lease_expires_at": 1200, "done": False}), {},
     {"action": "hold", "reason": "unknown_busy",
      "alerts_in": ["unknown_busy_report"]}),
    ("stale-generation done cannot stop current busy",
     st(last_user_input_at=0, generation=2,
        busy=busy(work_id="w2", lease=1200, generation=2)),
     sg(busy=busy(done=True, work_id="w1", generation=1)), {},
     {"action": "hold", "reason": "busy_protected",
      "alerts_in": ["stale_busy_report_ignored"]}),
    ("stale-generation report without claim is ignored",
     st(last_user_input_at=0, generation=2),
     sg(busy=busy(generation=1)), {},
     {"action": "request_idle", "busy_status": "none",
      "alerts_in": ["stale_busy_report_ignored"]}),
    ("missing busy hook is unknown_busy with alert",
     st(last_user_input_at=0), sg(busy_hook_installed=False), {},
     {"action": "hold", "reason": "unknown_busy",
      "alerts_in": ["busy_hook_missing"], "requires_human": True}),
    # ---- cooldown between demotions
    ("cooldown holds the next demotion",
     st(last_user_input_at=0, last_demotion_at=950), sg(), {},
     {"action": "hold", "reason": "demotion_cooldown"}),
    ("demotion proceeds once cooldown elapsed",
     st(last_user_input_at=0, last_demotion_at=879), sg(), {},
     {"action": "request_idle"}),
    ("future anchor never demotes", st(last_user_input_at=1100), sg(), {},
     {"action": "hold", "reason": "steady"}),
    # ---- conservative-when-uncertain on Active
    ("no anchor at all holds conservatively",
     st(last_user_input_at=None, last_wakeup_at=None, state_since=None),
     sg(), {}, {"action": "hold", "reason": "unknown_activity_anchor"}),
    ("unrecognized user_input value blocks demotion",
     st(last_user_input_at=0), sg(user_input="yes"), {},
     {"action": "hold", "reason": "uncertain_signal"}),
    ("unrecognized cpu value blocks demotion",
     st(last_user_input_at=0), sg(cpu_demand_milli="high"), {},
     {"action": "hold", "reason": "uncertain_signal"}),
    ("disabled auto_idle never demotes", st(last_user_input_at=0), sg(),
     {"auto_idle_enabled": False}, {"action": "hold",
                                    "reason": "auto_idle_disabled"}),
    ("None threshold disables the transition", st(last_user_input_at=0),
     sg(), {"idle_after_seconds": None},
     {"action": "hold", "reason": "auto_idle_disabled"}),
    # ---- Idle: wakes vs suspend timer
    ("idle within window holds", st(observed_state="Idle", state_since=990),
     sg(), {}, {"action": "hold", "reason": "steady"}),
    ("idle suspend timer fires", st(observed_state="Idle", state_since=0),
     sg(), {}, {"action": "request_suspend", "reason": "suspend_timer"}),
    ("boundary exactly suspend_after fires",
     st(observed_state="Idle", state_since=700), sg(), {},
     {"action": "request_suspend"}),
    ("user_input wakes Idle immediately",
     st(observed_state="Idle", state_since=0), sg(user_input=True), {},
     {"action": "wake_active", "reason": "user_input"}),
    ("cpu_demand wakes Idle", st(observed_state="Idle", state_since=0),
     sg(cpu_demand_milli=300), {},
     {"action": "wake_active", "reason": "cpu_demand"}),
    ("new_task wakes Idle", st(observed_state="Idle", state_since=0),
     sg(new_task=True), {}, {"action": "wake_active",
                            "reason": "new_task"}),
    ("ping does not wake Idle", st(observed_state="Idle", state_since=0),
     sg(ping=True), {}, {"action": "request_suspend"}),
    ("resize does not wake Idle", st(observed_state="Idle",
                                     state_since=0),
     sg(resize=True), {}, {"action": "request_suspend"}),
    ("output does not wake Idle", st(observed_state="Idle", state_since=0),
     sg(output=True), {}, {"action": "request_suspend"}),
    ("excluded process cpu does not wake Idle",
     st(observed_state="Idle", state_since=0),
     sg(cpu_demand_milli=900, cpu_demand_process="code-server"), {},
     {"action": "request_suspend"}),
    ("busy forbids Idle→Suspend (long task protection)",
     st(observed_state="Idle", state_since=0, busy=busy()), sg(), {},
     {"action": "hold", "reason": "busy_protected"}),
    ("unknown busy blocks Idle→Suspend",
     st(observed_state="Idle", state_since=0, busy=busy(lease=500)),
     sg(), {}, {"action": "hold", "reason": "unknown_busy"}),
    ("cooldown holds suspend too",
     st(observed_state="Idle", state_since=0, last_demotion_at=950),
     sg(), {}, {"action": "hold", "reason": "demotion_cooldown"}),
    ("manual input wake is never blocked by cooldown",
     st(observed_state="Idle", state_since=0, last_demotion_at=995),
     sg(user_input=True), {}, {"action": "wake_active"}),
    ("unrecognized new_task value blocks suspend",
     st(observed_state="Idle", state_since=0), sg(new_task=1), {},
     {"action": "hold", "reason": "uncertain_signal"}),
    ("wake works despite unknown busy",
     st(observed_state="Idle", state_since=0, busy=busy(lease=500)),
     sg(user_input=True), {}, {"action": "wake_active"}),
    ("disabled auto_suspend never suspends",
     st(observed_state="Idle", state_since=0), sg(),
     {"auto_suspend_enabled": False},
     {"action": "hold", "reason": "auto_suspend_disabled"}),
    # ---- guards: immunity
    ("pending operation blocks every auto transition",
     st(last_user_input_at=0, pending_operation={"op_type": "suspend"}),
     sg(user_input=True, new_task=True, cpu_demand_milli=900), {},
     {"action": "hold", "reason": "operation_in_flight"}),
    ("unknown observed state holds", st(observed_state="Zombie"), sg(),
     {}, {"action": "hold", "reason": "unknown_state"}),
    ("unknown clock holds", st(last_user_input_at=0), sg(now=None), {},
     {"action": "hold", "reason": "unknown_clock"}),
]
# every non-managed state is immune — Error/Lost included (2026-10-02)
for _state in ("Creating", "Suspending", "Suspend", "Resuming",
               "Destroying", "Destroyed", "Lost", "Error"):
    CASES.append((
        f"{_state} is immune to timers and wakeups",
        st(observed_state=_state, last_user_input_at=0),
        sg(user_input=True, new_task=True, cpu_demand_milli=900,
           cpu_demand_process=None), {},
        {"action": "hold", "reason": "no_auto_transitions"}))


class SignalMatrixTable(unittest.TestCase):
    """表格式測試: one row per (state × signal class × guard)."""

    def test_table(self):
        for name, state, signals, pol_over, expected in CASES:
            with self.subTest(case=name):
                policy = dict(BASE_POLICY)
                policy.update(pol_over)
                decision = decide(state, signals, policy)
                self.assertEqual(decision["action"], expected["action"],
                                 decision)
                if "reason" in expected:
                    self.assertEqual(decision["reason"], expected["reason"],
                                     decision)
                if "busy_status" in expected:
                    self.assertEqual(decision["busy_status"],
                                     expected["busy_status"], decision)
                if "requires_human" in expected:
                    self.assertEqual(decision["requires_human"],
                                     expected["requires_human"], decision)
                for alert in expected.get("alerts_in", []):
                    self.assertIn(alert, decision["alerts"], decision)

    def test_table_size_is_the_full_matrix(self):
        self.assertGreaterEqual(len(CASES), 40)


class TwoHourLowCpuTask(unittest.TestCase):
    """2h+ 低 CPU 長工作在 busy lease 保護下存活 (simulated long timeline)."""

    POL = {"idle_after_seconds": 60, "suspend_after_seconds": 300,
           "cpu_demand_threshold_milli": 250, "demotion_cooldown_seconds": 0,
           "excluded_processes": ()}

    def test_active_survives_two_hours_of_renewed_busy(self):
        state = st(last_user_input_at=0)
        demotions = 0
        for now in range(0, 2 * HOUR + 600, 30):
            report = busy(lease=now + 60)      # renewed every 30s, TTL 60s
            decision = decide(state, {"now": now, "cpu_demand_milli": 5,
                                      "busy": report}, self.POL)
            self.assertEqual(decision["action"], "hold", (now, decision))
            self.assertEqual(decision["busy_status"], "protected", (now,))
            demotions += decision["action"] != "hold"
        self.assertEqual(demotions, 0)
        # control: the same timeline WITHOUT busy demotes right after the
        # threshold — proves the timeline is far longer than the timer
        control = decide(st(last_user_input_at=0), {"now": 61,
                                                    "cpu_demand_milli": 5},
                         self.POL)
        self.assertEqual(control["action"], "request_idle")

    def test_idle_survives_two_hours_of_renewed_busy(self):
        state = st(observed_state="Idle", state_since=0)
        for now in range(0, 2 * HOUR + 600, 30):
            decision = decide(state, {"now": now, "cpu_demand_milli": 5,
                                      "busy": busy(lease=now + 60)},
                              self.POL)
            self.assertEqual(decision["action"], "hold", (now, decision))

    def test_download_style_task_survives_with_output_traffic(self):
        # output frames keep flowing; they are never activity, and busy
        # still protects: no suspend across the whole download
        state = st(observed_state="Idle", state_since=0)
        for now in range(0, HOUR, 10):
            decision = decide(state, {"now": now, "output": True,
                                      "busy": busy(lease=now + 30)},
                              self.POL)
            self.assertEqual(decision["action"], "hold", (now, decision))


class BusyLeaseExpiry(unittest.TestCase):
    """busy 過期轉 unknown／告警：不降級、不當作完成."""

    POL = {"idle_after_seconds": 60, "suspend_after_seconds": 300,
           "cpu_demand_threshold_milli": 250, "demotion_cooldown_seconds": 0}

    def test_renewals_stop_then_lease_expires_to_unknown(self):
        state = st(last_user_input_at=0,
                   busy=busy(lease=660))       # last renewal at 600
        for now in (600, 659):
            decision = decide(state, {"now": now}, self.POL)
            self.assertEqual(decision["busy_status"], "protected", (now,))
        for now in (660, 1000, 10 * HOUR):
            decision = decide(state, {"now": now}, self.POL)
            self.assertEqual(decision["action"], "hold", (now, decision))
            self.assertEqual(decision["reason"], "unknown_busy", (now,))
            self.assertEqual(decision["busy_status"], "unknown_busy")
            self.assertIn("busy_lease_expired", decision["alerts"])
            self.assertTrue(decision["requires_human"])
            self.assertNotEqual(decision["busy_status"], "complete")

    def test_expired_report_signal_is_also_unknown(self):
        decision = decide(st(last_user_input_at=0),
                          sg(busy=busy(lease=999)), self.POL)
        self.assertEqual(decision["reason"], "unknown_busy")
        self.assertIn("busy_lease_expired", decision["alerts"])


class ErrorLostPendingImmunity(unittest.TestCase):
    """Error/Lost／operation 進行中不得被 timer 蓋掉；不自動重播死亡前任務."""

    POL = dict(BASE_POLICY)

    def test_error_never_auto_transitions_even_with_everything(self):
        for signals in (sg(), sg(user_input=True), sg(new_task=True),
                        sg(cpu_demand_milli=900),
                        sg(user_input=True, cpu_demand_milli=900,
                           new_task=True)):
            decision = decide(st(observed_state="Error",
                                 last_user_input_at=0), signals, self.POL)
            self.assertEqual(decision["action"], "hold")
            self.assertEqual(decision["reason"], "no_auto_transitions")

    def test_lost_with_unknown_busy_still_does_nothing(self):
        decision = decide(st(observed_state="Lost",
                             last_user_input_at=0,
                             busy=busy(lease=1)), sg(), self.POL)
        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["reason"], "no_auto_transitions")

    def test_pending_op_clears_then_timer_resumes(self):
        pending = st(last_user_input_at=0,
                     pending_operation={"op_type": "suspend"})
        self.assertEqual(decide(pending, sg(), self.POL)["reason"],
                         "operation_in_flight")
        cleared = st(last_user_input_at=0, pending_operation=None)
        self.assertEqual(decide(cleared, sg(), self.POL)["action"],
                         "request_idle")


class CooldownVsManualInput(unittest.TestCase):
    """cooldown 只擋降級，絕不擋手動輸入喚醒."""

    POL = {"idle_after_seconds": 60, "suspend_after_seconds": 300,
           "cpu_demand_threshold_milli": 250,
           "demotion_cooldown_seconds": 120, "excluded_processes": ()}

    def test_full_timeline(self):
        # t=0: input; t=60: first demotion allowed (no prior demotion)
        self.assertEqual(
            decide(st(last_user_input_at=0), {"now": 59}, self.POL)[
                "action"], "hold")
        self.assertEqual(
            decide(st(last_user_input_at=0), {"now": 60}, self.POL)[
                "action"], "request_idle")
        # caller records the demotion; sandbox is Idle at 60
        idle = st(observed_state="Idle", state_since=60,
                  last_user_input_at=0, last_demotion_at=60)
        # t=65: manual input wakes IMMEDIATELY despite the cooldown
        woke = decide(idle, {"now": 65, "user_input": True}, self.POL)
        self.assertEqual((woke["action"], woke["reason"]),
                         ("wake_active", "user_input"))
        # caller wakes the sandbox; next demotion attempt at t=126 is
        # still inside the cooldown window since the t=60 demotion
        active = st(observed_state="Active", state_since=65,
                    last_user_input_at=65, last_demotion_at=60)
        self.assertEqual(
            decide(active, {"now": 126}, self.POL)["reason"],
            "demotion_cooldown")
        # t=181: cooldown elapsed (181-60 >= 120) and timer re-fired
        self.assertEqual(
            decide(active, {"now": 181}, self.POL)["action"], "request_idle")

    def test_zero_cooldown_never_blocks(self):
        pol = dict(self.POL)
        pol["demotion_cooldown_seconds"] = 0
        idle = st(observed_state="Idle", state_since=0,
                  last_demotion_at=999)
        self.assertEqual(decide(idle, sg(), pol)["action"],
                         "request_suspend")


class ServerCountdown(unittest.TestCase):
    """policy／countdown 由伺服器計算：純函式、未知輸入給 null."""

    def test_counts_down_from_last_signal(self):
        got = countdown(BASE_POLICY, 100, 0)
        self.assertEqual(got, {"idle_in": 0, "suspend_in": 200})
        got = countdown(BASE_POLICY, 0, 0)
        self.assertEqual(got, {"idle_in": 60, "suspend_in": 300})
        got = countdown(BASE_POLICY, 30, 0)
        self.assertEqual(got, {"idle_in": 30, "suspend_in": 270})

    def test_disabled_transitions_count_null(self):
        got = countdown({"idle_after_seconds": None,
                         "suspend_after_seconds": 300}, 0, 0)
        self.assertEqual(got, {"idle_in": None, "suspend_in": 300})
        got = countdown({}, 0, 0)
        self.assertEqual(got, {"idle_in": None, "suspend_in": None})

    def test_unknown_inputs_count_null(self):
        for now, last in ((None, 0), (0, None), (None, None)):
            self.assertEqual(countdown(BASE_POLICY, now, last),
                             {"idle_in": None, "suspend_in": None})

    def test_backward_clock_shows_full_window(self):
        self.assertEqual(countdown(BASE_POLICY, 0, 100),
                         {"idle_in": 60, "suspend_in": 300})

    def test_ping_resize_do_not_refresh_the_anchor(self):
        # liveness/layout frames arriving now never move last_signal: the
        # countdown keeps running (decide-side rows assert the demotion)
        # pings/resize arriving now never move last_signal (only user_input
        # anchors); assert via decide: ping+resize at the timer boundary do
        # not stop the Idle demotion
        out = decide({"observed_state": "Active", "last_user_input_at": 0},
                     {"now": 100, "ping": True, "resize": True},
                     dict(BASE_POLICY, idle_after_seconds=100))
        self.assertEqual(out["action"], "request_idle")


class PolicyValidation(unittest.TestCase):
    def test_default_policy_is_fully_conservative(self):
        # no thresholds snuck in: absent policy disables everything
        self.assertEqual(decide(st(last_user_input_at=0), sg(), {})[
            "reason"], "auto_idle_disabled")
        idle = st(observed_state="Idle", state_since=0)
        self.assertEqual(decide(idle, sg(), {})["reason"],
                         "auto_suspend_disabled")

    def test_invalid_policy_values_raise(self):
        for bad in ({"idle_after_seconds": 0}, {"idle_after_seconds": -5},
                    {"idle_after_seconds": "60"},
                    {"suspend_after_seconds": "x"},
                    {"cpu_demand_threshold_milli": -1},
                    {"demotion_cooldown_seconds": -1},
                    {"auto_idle_enabled": "yes"},
                    {"excluded_processes": "code-server"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    normalize_policy(bad)

    def test_purity_no_global_mutation(self):
        before = (set(ap.STATES), set(ap.AUTO_MANAGED))
        state, signals, policy = st(), sg(), dict(BASE_POLICY)
        decide(state, signals, policy)
        self.assertEqual(policy, BASE_POLICY)
        self.assertEqual(state, st())
        self.assertEqual(signals, sg())
        self.assertEqual(before, (set(ap.STATES), set(ap.AUTO_MANAGED)))


class MutationGuards(unittest.TestCase):
    """Each safeguard must be load-bearing: break it, the guarantee breaks."""

    def test_ping_resize_never_activity_guard(self):
        state = st(last_user_input_at=0)
        self.assertEqual(decide(state, sg(ping=True), BASE_POLICY)[
            "action"], "request_idle")          # separation holds
        with mock.patch.object(ap, "_signal_wakeup",
                               lambda sig, pol: ("user_input", False)):
            breached = decide(state, sg(ping=True), BASE_POLICY)
        self.assertEqual(breached["action"], "hold")   # bypassed: ping wakes

    def test_busy_protection_guard(self):
        state = st(last_user_input_at=0, busy=busy(lease=999))
        self.assertEqual(decide(state, sg(), BASE_POLICY)["reason"],
                         "unknown_busy")        # protection holds
        with mock.patch.object(ap, "_busy_status",
                               lambda s, g, n: ("complete", [], False)):
            breached = decide(state, sg(), BASE_POLICY)
        self.assertEqual(breached["action"], "request_idle")  # bypassed

    def test_unknown_anchor_conservatism_guard(self):
        state = st(last_user_input_at=None, last_wakeup_at=None,
                   state_since=None)
        self.assertEqual(decide(state, sg(), BASE_POLICY)["reason"],
                         "unknown_activity_anchor")
        with mock.patch.object(ap, "_activity_anchor",
                               lambda s, g, w: 0):
            breached = decide(state, sg(), BASE_POLICY)
        self.assertEqual(breached["action"], "request_idle")  # bypassed

    def test_cooldown_guard(self):
        state = st(last_user_input_at=0, last_demotion_at=950)
        self.assertEqual(decide(state, sg(), BASE_POLICY)["reason"],
                         "demotion_cooldown")
        with mock.patch.object(ap, "_cooldown_blocks",
                               lambda s, n, p: False):
            breached = decide(state, sg(), BASE_POLICY)
        self.assertEqual(breached["action"], "request_idle")  # bypassed

    def test_error_lost_immunity_guard(self):
        state = st(observed_state="Error", last_user_input_at=0)
        self.assertEqual(decide(state, sg(), BASE_POLICY)["reason"],
                         "no_auto_transitions")
        with mock.patch.object(ap, "_auto_managed", lambda observed: True):
            breached = decide(state, sg(), BASE_POLICY)
        self.assertEqual(breached["action"], "request_suspend")  # bypassed

    def test_pending_operation_guard(self):
        state = st(last_user_input_at=0,
                   pending_operation={"op_type": "suspend"})
        self.assertEqual(decide(state, sg(), BASE_POLICY)["reason"],
                         "operation_in_flight")
        with mock.patch.object(ap, "_pending_operation", lambda s: False):
            breached = decide(state, sg(), BASE_POLICY)
        self.assertEqual(breached["action"], "request_idle")  # bypassed

    def test_ide_exclusion_guard(self):
        state = st(last_user_input_at=0)
        signals = sg(cpu_demand_milli=900, cpu_demand_process="code-server")
        self.assertEqual(decide(state, signals, BASE_POLICY)["action"],
                         "request_idle")        # exclusion holds
        with mock.patch.object(ap, "_cpu_excluded",
                               lambda source, pol: False):
            breached = decide(state, signals, BASE_POLICY)
        self.assertEqual(breached["action"], "hold")   # bypassed: wakes


if __name__ == "__main__":
    print(f"auto-policy table rows: {len(CASES)}")
    unittest.main()
