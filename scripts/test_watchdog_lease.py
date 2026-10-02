"""Guard tests for the #18 watchdog lease slice.

Covers the issue acceptance without a host or Runner: lease expiry math
(renew extends, missed heartbeat expires), stale-epoch renew rejection
(the old runner's lease cannot extend past the new instance's),
detect→confirm stop discipline (Lost/Unknown between detect and confirm,
no billing cutoff before confirmed stop, a dead runner's report is not
proof), the watchdog-itself-failed alert path, three-timestamp ordering
(detected ≤ confirmed_stopped = billing_cutoff, equality allowed), quota
restore that NEVER emits "max" plus exact cpu/memory/pids builders,
network-failure isolation, and mutation guards proving each safeguard is
load-bearing (pattern per scripts/test_operation_reconcile.py).
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import watchdog_lease as wl  # noqa: E402
from watchdog_lease import (  # noqa: E402
    Lease, LeaseExpired, QuotaRestoreError, StaleEpoch, WatchdogPolicy,
    memory_max, network_failure_policy, pids_max, quota_restore)


class FakeClock:
    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now += ms
        return self.now


def expired_state(detected_at=None, evidence=None, epoch=2):
    return {"lease_expired": True, "detected_at": detected_at,
            "epoch": epoch, "stop_evidence": evidence}


class LeaseExpiryMath(unittest.TestCase):
    def test_issue_sets_deadline_from_injected_clock(self):
        clock = FakeClock(now=1000)
        lease = Lease("sbx1", clock, ttl_ms=30000)
        view = lease.issue()
        self.assertEqual(view["epoch"], 1)
        self.assertEqual(view["deadline_at"], 31000)
        self.assertFalse(lease.expired_at(30999))

    def test_heartbeat_renew_extends_deadline(self):
        clock = FakeClock()
        lease = Lease("sbx1", clock, ttl_ms=30000)
        lease.issue()
        clock.advance(20000)
        view = lease.renew(1)
        self.assertEqual(view["deadline_at"], 50000)   # 20000 + 30000
        self.assertFalse(lease.expired_at(49999))

    def test_missed_heartbeat_expires(self):
        clock = FakeClock()
        lease = Lease("sbx1", clock, ttl_ms=30000)
        lease.issue()
        self.assertFalse(lease.expired_at(29999))       # half-open bound
        self.assertTrue(lease.expired_at(30000))
        clock.advance(30000)
        self.assertTrue(lease.expired_at())

    def test_renew_after_expiry_is_fenced_not_resurrected(self):
        clock = FakeClock()
        lease = Lease("sbx1", clock, ttl_ms=30000)
        lease.issue()
        clock.advance(31000)
        with self.assertRaises(LeaseExpired):
            lease.renew(1)   # recovery is a new epoch, not a zombie renew

    def test_expired_at_uses_clock_when_now_omitted(self):
        clock = FakeClock()
        lease = Lease("sbx1", clock, ttl_ms=30000)
        lease.issue()
        clock.now = 29999
        self.assertFalse(lease.expired_at())
        clock.advance(1)
        self.assertTrue(lease.expired_at())

    def test_renew_requires_issued_lease(self):
        lease = Lease("sbx1", FakeClock())
        with self.assertRaises(ValueError):
            lease.renew(1)


class LeaseEpochFencing(unittest.TestCase):
    def test_epochs_increase_monotonically_per_sandbox(self):
        lease = Lease("sbx1", FakeClock())
        self.assertEqual([lease.issue()["epoch"] for _ in range(3)],
                         [1, 2, 3])

    def test_stale_epoch_renew_rejected_after_recreation(self):
        clock = FakeClock()
        lease = Lease("sbx1", clock, ttl_ms=10000)
        lease.issue()                    # epoch 1, old runner
        lease.issue()                    # epoch 2, recreated runner
        with self.assertRaises(StaleEpoch):
            lease.renew(1)               # old runner wakes up: fenced
        view = lease.renew(2)            # current instance still renews
        self.assertEqual(view["epoch"], 2)

    def test_old_runner_cannot_extend_new_instance_deadline(self):
        clock = FakeClock(now=5000)
        lease = Lease("sbx1", clock, ttl_ms=10000)
        lease.issue()                    # epoch 1, deadline 15000
        lease.issue()                    # epoch 2, deadline 15000
        clock.now = 14000
        with self.assertRaises(StaleEpoch):
            lease.renew(1)
        # the failed stale renew did NOT move the current deadline
        self.assertTrue(lease.expired_at(15000))
        clock.now = 14000
        lease.renew(2)
        self.assertFalse(lease.expired_at(23999))   # moved to 24000


class WatchdogDetectAndConfirm(unittest.TestCase):
    def setUp(self):
        self.policy = WatchdogPolicy(confirm_timeout_ms=10000)

    def test_healthy_lease_no_action_no_timestamps(self):
        decision = self.policy.evaluate(
            5000, {"lease_expired": False, "detected_at": None,
                   "epoch": 2, "stop_evidence": None}, True)
        self.assertEqual(decision["phase"], "healthy")
        self.assertIsNone(decision["display_state"])
        self.assertIsNone(decision["detected_at"])
        self.assertIsNone(decision["billing_cutoff_at"])
        self.assertFalse(decision["release_capacity"])

    def test_detect_records_t_detect_and_shows_lost(self):
        decision = self.policy.evaluate(
            5000, expired_state(detected_at=None), runner_reachable=False)
        self.assertEqual(decision["phase"], "lost_unconfirmed")
        self.assertEqual(decision["detected_at"], 5000)
        self.assertEqual(decision["display_state"], "Lost")   # not Active
        self.assertIsNone(decision["billing_cutoff_at"])

    def test_detect_to_confirm_gap_is_lost_and_not_free(self):
        decision = self.policy.evaluate(
            8000, expired_state(detected_at=5000), runner_reachable=False)
        self.assertEqual(decision["phase"], "lost_unconfirmed")
        self.assertEqual(decision["display_state"], "Lost")
        self.assertFalse(decision["release_capacity"])
        self.assertIsNone(decision["billing_cutoff_at"])
        self.assertIn("collect_stop_evidence", decision["actions"])

    def test_confirmed_stop_sets_cutoff_and_releases(self):
        decision = self.policy.evaluate(
            9000, expired_state(detected_at=5000,
                                evidence={"source": "watchdog_kill_confirmed",
                                          "at": 9000, "epoch": 2}),
            runner_reachable=False)
        self.assertEqual(decision["phase"], "confirmed_stopped")
        self.assertEqual(decision["confirmed_stopped_at"], 9000)
        self.assertEqual(decision["billing_cutoff_at"], 9000)
        self.assertTrue(decision["release_capacity"])

    def test_host_observation_is_proof_while_runner_dead(self):
        decision = self.policy.evaluate(
            9000, expired_state(detected_at=5000,
                                evidence={"source": "host_observed_stopped",
                                          "at": 8800, "epoch": 2}),
            runner_reachable=False)
        self.assertEqual(decision["phase"], "confirmed_stopped")

    def test_live_runner_confirmation_is_proof(self):
        decision = self.policy.evaluate(
            9000, expired_state(detected_at=5000,
                                evidence={"source": "runner_confirmed_stop",
                                          "at": 8800, "epoch": 2}),
            runner_reachable=True)
        self.assertEqual(decision["phase"], "confirmed_stopped")

    def test_evidence_before_detection_clamps_to_keep_order(self):
        decision = self.policy.evaluate(
            5000, expired_state(detected_at=5000,
                                evidence={"source": "watchdog_kill_confirmed",
                                          "at": 3000, "epoch": 2}),
            runner_reachable=False)
        self.assertEqual(decision["confirmed_stopped_at"], 5000)

    def test_persistent_loss_escalates_to_human(self):
        before = self.policy.evaluate(
            9999, expired_state(detected_at=0), runner_reachable=False)
        self.assertFalse(before["requires_human"])
        after = self.policy.evaluate(
            10000, expired_state(detected_at=0), runner_reachable=False)
        self.assertTrue(after["requires_human"])
        self.assertIn("stop_unconfirmed_escalation", after["alerts"])


class DeadRunnerNotProof(unittest.TestCase):
    def setUp(self):
        self.policy = WatchdogPolicy()

    def test_dead_runner_report_is_not_proof_of_suspend(self):
        decision = self.policy.evaluate(
            9000, expired_state(detected_at=5000,
                                evidence={"source": "runner_confirmed_stop",
                                          "at": 8800, "epoch": 2}),
            runner_reachable=False)
        self.assertEqual(decision["phase"], "lost_unconfirmed")
        self.assertEqual(decision["evidence_rejected"],
                         "dead_runner_not_proof")
        self.assertIsNone(decision["billing_cutoff_at"])
        self.assertFalse(decision["release_capacity"])

    def test_runner_unreachable_alone_is_not_confirmation(self):
        decision = self.policy.evaluate(
            9000, expired_state(detected_at=5000), runner_reachable=False)
        self.assertEqual(decision["phase"], "lost_unconfirmed")
        self.assertIsNone(decision["billing_cutoff_at"])

    def test_unknown_evidence_source_rejected(self):
        decision = self.policy.evaluate(
            9000, expired_state(detected_at=5000,
                                evidence={"source": "trust_me", "at": 8800}),
            runner_reachable=True)
        self.assertEqual(decision["evidence_rejected"], "unknown_proof_source")
        self.assertEqual(decision["phase"], "lost_unconfirmed")

    def test_stale_epoch_evidence_is_not_proof(self):
        decision = self.policy.evaluate(
            9000, expired_state(detected_at=5000, epoch=2,
                                evidence={"source": "host_observed_stopped",
                                          "at": 8800, "epoch": 1}),
            runner_reachable=False)
        self.assertEqual(decision["evidence_rejected"],
                         "stale_epoch_not_proof")
        self.assertIsNone(decision["billing_cutoff_at"])


class WatchdogItselfFailed(unittest.TestCase):
    def test_distinct_flag_alert_and_human_path(self):
        alert = WatchdogPolicy().watchdog_unreachable_alert(now=7000)
        self.assertEqual(alert["flag"], "watchdog_failed")
        self.assertEqual(alert["phase"], "watchdog_unreachable")
        self.assertEqual(alert["display_state"], "Unknown")
        self.assertTrue(alert["requires_human"])
        self.assertEqual(alert["actions"], ["human_investigation"])

    def test_watchdog_failure_never_stops_billing_or_releases(self):
        alert = WatchdogPolicy().watchdog_unreachable_alert(now=7000)
        self.assertIsNone(alert["confirmed_stopped_at"])
        self.assertIsNone(alert["billing_cutoff_at"])
        self.assertFalse(alert["release_capacity"])
        # distinct from lease alerts: those never carry the watchdog flag
        lease_decision = WatchdogPolicy().evaluate(
            7000, expired_state(detected_at=None), runner_reachable=False)
        self.assertNotIn("flag", lease_decision)


class ThreeTimestampsOrder(unittest.TestCase):
    def setUp(self):
        self.policy = WatchdogPolicy()

    def test_order_detected_le_confirmed_le_cutoff(self):
        decision = self.policy.evaluate(
            9000, expired_state(detected_at=5000,
                                evidence={"source": "watchdog_kill_confirmed",
                                          "at": 9000, "epoch": 2}),
            runner_reachable=False)
        self.assertLessEqual(decision["detected_at"],
                             decision["confirmed_stopped_at"])
        self.assertLessEqual(decision["confirmed_stopped_at"],
                             decision["billing_cutoff_at"])
        self.assertEqual(decision["billing_cutoff_at"],
                         decision["confirmed_stopped_at"])  # cutoff = stop

    def test_equality_allowed(self):
        decision = self.policy.evaluate(
            5000, expired_state(detected_at=5000,
                                evidence={"source": "watchdog_kill_confirmed",
                                          "at": 5000, "epoch": 2}),
            runner_reachable=False)
        self.assertEqual((decision["detected_at"],
                          decision["confirmed_stopped_at"],
                          decision["billing_cutoff_at"]), (5000, 5000, 5000))

    def test_unconfirmed_phases_have_no_cutoff_or_stop_time(self):
        for decision in (
            WatchdogPolicy().evaluate(8000, expired_state(detected_at=5000),
                                      runner_reachable=False),
            WatchdogPolicy().watchdog_unreachable_alert(now=8000),
            network_failure_policy("cleanup", now=8000)):
            label = decision.get("phase") or decision["action"]
            with self.subTest(phase=label):
                self.assertIsNone(decision["confirmed_stopped_at"])
                self.assertIsNone(decision["billing_cutoff_at"])


class QuotaBuilders(unittest.TestCase):
    def test_cpu_restore_exact_purchased_values(self):
        # cpu.max is "$MAX $PERIOD" in µs: MAX = milli * period / 1000
        self.assertEqual(quota_restore(2000), "200000 100000")
        self.assertEqual(quota_restore(500), "50000 100000")
        self.assertEqual(quota_restore(1), "100 100000")
        self.assertEqual(quota_restore(2000, period_us=250000),
                         "500000 250000")

    def test_cpu_restore_never_emits_max(self):
        for cpu_milli in (1, 100, 500, 2000, 64000):
            value = quota_restore(cpu_milli)
            self.assertNotEqual(value.split(" ")[0], "max")
            self.assertNotIn("max", value)

    def test_memory_max_exact_values(self):
        self.assertEqual(memory_max(2 * 1024 ** 3), "2147483648")
        self.assertEqual(memory_max(1), "1")

    def test_pids_max_exact_values(self):
        self.assertEqual(pids_max(512), "512")
        self.assertEqual(pids_max(0), "0")

    def test_invalid_quota_inputs_raise(self):
        for bad in (0, -1, "2000", 2.0, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    quota_restore(bad)
                with self.assertRaises(ValueError):
                    memory_max(bad)
        for bad in (-1, "5", 1.5, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    pids_max(bad)


class NetworkFailure(unittest.TestCase):
    def test_cleanup_failure_blocks_and_isolates(self):
        decision = network_failure_policy(
            "cleanup", error="iptables flush timeout", now=9000)
        self.assertEqual(decision["action"], "block_and_isolate")
        self.assertFalse(decision["destroy"])
        self.assertTrue(decision["keep_volumes"])
        self.assertTrue(decision["requires_human"])
        self.assertIn("network_cleanup_failed", decision["alerts"])

    def test_flush_failure_same_isolation_policy(self):
        decision = network_failure_policy("flush", now=9000)
        self.assertEqual(decision["action"], "block_and_isolate")
        self.assertTrue(decision["keep_volumes"])

    def test_never_returns_destroy(self):
        for stage in ("cleanup", "flush"):
            decision = network_failure_policy(stage)
            self.assertNotEqual(decision["action"], "destroy")
            self.assertFalse(decision["destroy"])


class MutationGuards(unittest.TestCase):
    """Each safeguard must be load-bearing: break it, the guarantee breaks."""

    def test_stale_epoch_fence_guard(self):
        clock = FakeClock(now=1000)
        lease = Lease("sbx1", clock, ttl_ms=30000)
        lease.issue()
        lease.issue()                    # epoch 2, deadline 31000
        with self.assertRaises(StaleEpoch):   # fence holds
            lease.renew(1)
        clock.advance(5000)
        with mock.patch.object(Lease, "_epoch_ok",
                               lambda self, a, b: True):
            lease.renew(1)               # guard bypassed: stale renew lands
        self.assertEqual(lease.lease()["deadline_at"], 36000)

    def test_zombie_resurrection_guard(self):
        clock = FakeClock()
        lease = Lease("sbx1", clock, ttl_ms=30000)
        lease.issue()
        clock.advance(30000)
        with self.assertRaises(LeaseExpired):  # expiry fence holds
            lease.renew(1)
        with mock.patch.object(Lease, "expired_at",
                               lambda self, now=None: False):
            lease.renew(1)               # guard bypassed: lease resurrects
        self.assertEqual(lease.lease()["deadline_at"], 60000)

    def test_dead_runner_not_proof_guard(self):
        policy = WatchdogPolicy()
        state = expired_state(detected_at=5000,
                              evidence={"source": "runner_confirmed_stop",
                                        "at": 8800, "epoch": 2})
        self.assertEqual(
            policy.evaluate(9000, state, runner_reachable=False)["phase"],
            "lost_unconfirmed")          # rule holds
        with mock.patch.object(WatchdogPolicy, "_admit",
                               lambda self, ev, reach, epoch: (ev, None)):
            breached = policy.evaluate(9000, state, runner_reachable=False)
        self.assertEqual(breached["phase"], "confirmed_stopped")  # bypassed
        self.assertIsNotNone(breached["billing_cutoff_at"])

    def test_billing_cutoff_only_at_confirmed_stop_guard(self):
        policy, state = WatchdogPolicy(), expired_state(detected_at=5000)
        real_evaluate = WatchdogPolicy.evaluate

        def cutting_at_detect(self, now, lease_state, runner_reachable):
            decision = real_evaluate(self, now, lease_state,
                                     runner_reachable)
            decision["billing_cutoff_at"] = decision["detected_at"]
            return decision
        with mock.patch.object(WatchdogPolicy, "evaluate", cutting_at_detect):
            broken = policy.evaluate(9000, state, runner_reachable=False)
        self.assertIsNotNone(broken["billing_cutoff_at"])   # bug: cuts early
        real = policy.evaluate(9000, state, runner_reachable=False)
        self.assertIsNone(real["billing_cutoff_at"])        # invariant holds

    def test_capacity_not_released_before_confirmation_guard(self):
        policy, state = WatchdogPolicy(), expired_state(detected_at=5000)
        real_evaluate = WatchdogPolicy.evaluate

        def releasing_at_detect(self, now, lease_state, runner_reachable):
            decision = real_evaluate(self, now, lease_state,
                                     runner_reachable)
            decision["release_capacity"] = True
            return decision
        with mock.patch.object(WatchdogPolicy, "evaluate", releasing_at_detect):
            broken = policy.evaluate(9000, state, runner_reachable=False)
        self.assertTrue(broken["release_capacity"])   # bug: free while Lost
        real = policy.evaluate(9000, state, runner_reachable=False)
        self.assertFalse(real["release_capacity"])    # not free unconfirmed

    def test_quota_never_max_guard(self):
        self.assertEqual(quota_restore(2000), "200000 100000")
        with mock.patch.object(wl, "_quota_max_us",
                               lambda cpu_milli, period_us: "max"):
            with self.assertRaises(QuotaRestoreError):  # guard holds
                quota_restore(2000)
            with mock.patch.object(wl, "_reject_unlimited",
                                   lambda value: value):
                self.assertEqual(quota_restore(2000), "max 100000")
                # guard bypassed: the unlimited restore flows through

    def test_network_failure_never_destroys_guard(self):
        real = network_failure_policy("cleanup")
        self.assertEqual(real["action"], "block_and_isolate")
        self.assertFalse(real["destroy"])
        with mock.patch.object(wl, "ISOLATION_ACTION", "destroy"):
            broken = network_failure_policy("cleanup")
        self.assertEqual(broken["action"], "destroy")  # the forbidden value
        self.assertNotEqual(network_failure_policy("cleanup")["action"],
                            "destroy")


if __name__ == "__main__":
    unittest.main()
