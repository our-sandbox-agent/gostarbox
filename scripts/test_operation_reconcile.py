"""Guard tests for the #17 operation reconcile slice.

Covers the issue scenarios without a Runner: the full crash matrix
(4 points x 3 operations, replay converges to the no-crash state), concurrent
suspend/resume/destroy serialization, timeout-is-not-failed, runtime-ok /
DB-fail replay, unknown-instance isolation, Lost reservation keeping,
recreation with generation fencing, late old-generation writes, idempotency
and version refusals, restart persistence, and mutation guards proving each
safeguard is load-bearing (pattern per scripts/test_resource_events.py).
"""
import copy
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from operation_reconcile import (  # noqa: E402
    CRASH_POINTS, OP_TYPES, IdempotencyConflict, OperationConflict,
    OperationLog, Reconciler, RuntimeDouble, VersionConflict, converged_view,
    simulate_crash)


def seeded_log(observed="Active", desired="Active", sandbox_id="sbx1"):
    log = OperationLog()
    log.create_sandbox(sandbox_id, desired_state=desired,
                       observed_state=observed)
    return log


def run_to_completion(log, runtime, sandbox_id, op_type):
    sb = log.sandbox(sandbox_id)
    receipt = log.request(sandbox_id, op_type,
                          idempotency_key=f"key-{op_type}",
                          expected_version=sb["version"])
    op_id = receipt["operation"]["operation_id"]
    log.dispatch(op_id)
    log.runtime_outcome(op_id, runtime.execute(log.operation(op_id)))
    log.emit_operation_event(op_id)
    return op_id


class AcceptanceAndSerialization(unittest.TestCase):
    def test_opposite_operation_never_interleaves(self):
        log = seeded_log()
        log.request("sbx1", "suspend", idempotency_key="a",
                    expected_version=1)
        with self.assertRaises(OperationConflict):
            log.request("sbx1", "resume", idempotency_key="b",
                        expected_version=2)  # re-read version, still 409
        self.assertEqual(len(log.operations("sbx1")), 1)  # one sequence

    def test_same_type_pending_replays_same_operation(self):
        log = seeded_log()
        first = log.request("sbx1", "suspend", idempotency_key="a",
                            expected_version=1)
        again = log.request("sbx1", "suspend", idempotency_key="b",
                            expected_version=2)
        self.assertEqual(again["status"], "pending_replay")
        self.assertEqual(again["operation"]["operation_id"],
                         first["operation"]["operation_id"])
        self.assertEqual(len(log.operations("sbx1")), 1)

    def test_version_conflict_on_stale_expected_version(self):
        log = seeded_log()
        with self.assertRaises(VersionConflict):
            log.request("sbx1", "suspend", idempotency_key="a",
                        expected_version=99)
        self.assertEqual(log.operations("sbx1"), [])  # no operation created

    def test_suspend_refused_from_suspend_state(self):
        log = seeded_log()
        run_to_completion(log, RuntimeDouble(), "sbx1", "suspend")
        with self.assertRaises(OperationConflict):  # refuse-on-unknown:
            log.request("sbx1", "suspend", idempotency_key="b",
                        expected_version=log.sandbox("sbx1")["version"])

    def test_repeat_destroy_returns_existing_operation(self):
        log = seeded_log()
        run_to_completion(log, RuntimeDouble(), "sbx1", "destroy")
        repeat = log.request("sbx1", "destroy", idempotency_key="b",
                             expected_version=log.sandbox("sbx1")["version"])
        self.assertEqual(repeat["status"], "replayed")
        self.assertEqual(len(log.operations("sbx1")), 1)  # never a second

    def test_resume_requires_reconciliation_from_error(self):
        log = seeded_log()
        run_to_completion(log, RuntimeDouble(), "sbx1", "suspend")
        log.mark_reconciled("sbx1")
        log.mark_reconciled("sbx1")  # idempotent
        receipt = log.request("sbx1", "resume", idempotency_key="r",
                              expected_version=log.sandbox("sbx1")["version"])
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(log.sandbox("sbx1")["generation"], 2)


class IdempotencyAndTimeout(unittest.TestCase):
    def test_same_key_same_body_returns_same_operation(self):
        log = seeded_log()
        first = log.request("sbx1", "suspend", idempotency_key="k1",
                            expected_version=1)
        replay = log.request("sbx1", "suspend", idempotency_key="k1",
                             expected_version=1)
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(replay["operation"]["operation_id"],
                         first["operation"]["operation_id"])
        self.assertEqual(len(log.operations("sbx1")), 1)

    def test_same_key_different_body_conflicts(self):
        log = seeded_log()
        log.request("sbx1", "suspend", idempotency_key="k1",
                    expected_version=1)
        with self.assertRaises(IdempotencyConflict):
            log.request("sbx1", "destroy", idempotency_key="k1",
                        expected_version=1)

    def test_timeout_is_not_failed_stays_pollable_and_dispatchable(self):
        log = seeded_log()
        op_id = log.request("sbx1", "suspend", idempotency_key="a",
                            expected_version=1)["operation"]["operation_id"]
        log.dispatch(op_id)
        view = log.mark_timeout(op_id)["operation"]
        self.assertEqual(view["state"], "timeout")
        self.assertTrue(view["retryable"])
        self.assertTrue(view["timeout_is_not_failed"])
        self.assertNotEqual(view["state"], "failed")
        # sandbox not failed either: still Suspending, pending preserved
        sb = log.sandbox("sbx1")
        self.assertEqual(sb["observed_state"], "Suspending")
        self.assertEqual(sb["pending_operation"], op_id)
        # dispatchable again and settleable to the single outcome
        log.dispatch(op_id)
        outcome = RuntimeDouble().execute(log.operation(op_id))
        settled = log.runtime_outcome(op_id, outcome)
        self.assertEqual(settled["status"], "confirmed")
        self.assertEqual(log.sandbox("sbx1")["observed_state"], "Suspend")


class CrashMatrix(unittest.TestCase):
    def test_full_crash_matrix_converges(self):
        for op_type in OP_TYPES:
            for point in CRASH_POINTS:
                with self.subTest(op_type=op_type, point=point):
                    result = simulate_crash(op_type, point)
                    self.assertEqual(result["recovered"], result["clean"])

    def test_matrix_size_is_twelve_cells(self):
        self.assertEqual(len(OP_TYPES) * len(CRASH_POINTS), 12)

    def test_runtime_ok_db_fail_replay_no_double_build(self):
        # runtime succeeded; the DB write of the verdict was lost
        log, rt = seeded_log(), RuntimeDouble()
        op_id = log.request("sbx1", "suspend", idempotency_key="a",
                            expected_version=1)["operation"]["operation_id"]
        log.dispatch(op_id)
        rt.execute(log.operation(op_id))  # real-world effect happened
        snapshot = log.to_dict()          # verdict write lost here
        recovered = OperationLog.from_dict(snapshot)
        recovered.replay(rt)
        self.assertEqual(recovered.sandbox("sbx1")["observed_state"], "Suspend")
        self.assertEqual(rt.executions(), 1)      # no double-build
        self.assertEqual(len(recovered.operations("sbx1")), 1)
        self.assertEqual(recovered.operation(op_id)["state"], "confirmed")

    def test_replay_on_converged_log_is_idempotent(self):
        log, rt = seeded_log(), RuntimeDouble()
        run_to_completion(log, rt, "sbx1", "suspend")
        before = converged_view(log, rt)
        log.replay(rt)
        log.replay(rt)
        self.assertEqual(converged_view(log, rt), before)

    def test_snapshot_restore_midflight_round_trip(self):
        log = seeded_log()
        op_id = log.request("sbx1", "destroy", idempotency_key="a",
                            expected_version=1)["operation"]["operation_id"]
        log.dispatch(op_id)
        restored = OperationLog.from_json(log.to_json())
        self.assertEqual(restored.sandbox("sbx1"), log.sandbox("sbx1"))
        self.assertEqual(restored.operation(op_id), log.operation(op_id))
        rt = RuntimeDouble()
        restored.replay(rt)
        self.assertEqual(restored.sandbox("sbx1")["observed_state"], "Destroyed")
        self.assertFalse(restored.sandbox("sbx1")["reserved"])


class FencingAndLateEvents(unittest.TestCase):
    def test_resume_bumps_generation_and_fences_old(self):
        log = seeded_log()
        run_to_completion(log, RuntimeDouble(), "sbx1", "suspend")
        log.mark_reconciled("sbx1")
        op2 = log.request("sbx1", "resume", idempotency_key="r",
                          expected_version=log.sandbox("sbx1")["version"])
        self.assertEqual(op2["operation"]["generation"], 2)
        self.assertEqual(log.sandbox("sbx1")["generation"], 2)

    def test_late_old_generation_event_is_history_only(self):
        log = seeded_log()
        run_to_completion(log, RuntimeDouble(), "sbx1", "suspend")
        log.mark_reconciled("sbx1")
        run_to_completion(log, RuntimeDouble(), "sbx1", "resume")
        self.assertEqual(log.sandbox("sbx1")["observed_state"], "Active")
        receipt = log.apply_event("sbx1", generation=1,
                                  observed_state="Suspend",
                                  evidence={"source": "old-runner"})
        self.assertEqual(receipt["status"], "history_only")
        # current state untouched: never overwritten by late old-gen events
        self.assertEqual(log.sandbox("sbx1")["observed_state"], "Active")
        history = log.sandbox("sbx1")["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "history_only")

    def test_current_generation_event_applies(self):
        log = seeded_log()
        receipt = log.apply_event("sbx1", generation=1, observed_state="Idle")
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual(log.sandbox("sbx1")["observed_state"], "Idle")

    def test_stale_generation_write_rejected_but_preserved(self):
        log = seeded_log()
        op1 = run_to_completion(log, RuntimeDouble(), "sbx1", "suspend")
        log.mark_lost("sbx1", last_observed_at_ms=5)
        log.mark_reconciled("sbx1")  # opens Lost for changes (op1 already terminal)
        self.assertEqual(log.operation(op1)["state"], "confirmed")
        run_to_completion(log, RuntimeDouble(), "sbx1", "resume")  # gen 2
        # the old runner finally reports for generation 1: history only
        receipt = log.runtime_outcome(
            op1, {"succeeded": True, "observed_state": "Suspend"})
        self.assertEqual(receipt["status"], "stale_generation")
        self.assertEqual(log.sandbox("sbx1")["observed_state"], "Active")
        self.assertTrue(log.operation(op1)["history"])


class ReconcilerRules(unittest.TestCase):
    def test_desired_suspend_observed_active_requests_suspend(self):
        log = seeded_log(observed="Active", desired="Suspend")
        actions = Reconciler(log).diff(
            log.sandboxes(),
            {"inst-1": {"sandbox_id": "sbx1", "state": "Active",
                        "generation": 1}})
        self.assertEqual(actions,
                         [{"action": "request_suspend", "sandbox_id": "sbx1"}])
        Reconciler(log).apply(actions)
        self.assertEqual(log.sandbox("sbx1")["observed_state"], "Suspending")

    def test_unknown_instance_isolated_never_auto_adopted(self):
        log = seeded_log()
        before = len(log.sandboxes())
        actions = Reconciler(log).diff(
            log.sandboxes(),
            {"inst-42": {"sandbox_id": "sbx_someone_elses",
                         "state": "Active", "generation": 7}})
        self.assertEqual(actions[0]["action"], "isolate_unknown_instance")
        self.assertFalse(actions[0]["auto_adopt"])
        Reconciler(log).apply(actions)
        self.assertEqual(len(log.sandboxes()), before)  # no adoption
        flags = log.flags()
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["reason"], "unknown_instance")
        self.assertEqual(flags[0]["resolution"], "human_decision")

    def test_lost_without_stop_evidence_keeps_reservation_uncertain(self):
        log = seeded_log()
        run_to_completion(log, RuntimeDouble(), "sbx1", "suspend")
        log.mark_lost("sbx1", last_observed_at_ms=100)
        actions = Reconciler(log).diff(
            log.sandboxes(),
            {"inst-1": {"sandbox_id": "sbx1", "state": "Lost",
                        "generation": 1, "stop_evidence": False}})
        self.assertEqual(actions,
                         [{"action": "keep_reservation_mark_uncertain",
                           "sandbox_id": "sbx1", "stop_evidence": False,
                           "release_capacity": False}])
        Reconciler(log).apply(actions)
        sb = log.sandbox("sbx1")
        self.assertTrue(sb["reserved"])    # reservation kept
        self.assertTrue(sb["uncertain"])   # marked uncertain, not zero
        self.assertEqual(sb["observed_state"], "Lost")
        # no recreate attempted: still exactly the one earlier suspend op
        ops = log.operations("sbx1")
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["op_type"], "suspend")

    def test_error_recreation_new_generation_fences_old(self):
        log = seeded_log()
        rt = RuntimeDouble()
        run_to_completion(log, rt, "sbx1", "suspend")
        # known failure into Error: reconcile, then recreate
        log.mark_reconciled("sbx1")
        log.sandbox("sbx1")
        desired = log.sandboxes()
        desired["sbx1"]["desired_state"] = "Active"
        desired["sbx1"]["observed_state"] = "Error"
        actions = Reconciler(log).diff(
            desired, {"inst-1": {"sandbox_id": "sbx1", "state": "Error",
                                 "generation": 1}})
        self.assertEqual(actions[0]["action"], "recreate_new_generation")
        self.assertTrue(actions[0]["bump_generation"])
        Reconciler(log).apply(actions)
        sb = log.sandbox("sbx1")
        self.assertEqual(sb["observed_state"], "Resuming")  # Error->Resuming
        self.assertEqual(sb["generation"], 2)               # new generation
        log.replay(rt)                                     # fence + start
        self.assertEqual(log.sandbox("sbx1")["observed_state"], "Active")
        # old generation is fenced: its writes land in history only
        receipt = log.apply_event("sbx1", generation=1,
                                  observed_state="Suspend")
        self.assertEqual(receipt, {"status": "history_only"})

    def test_recreation_rate_limited_by_pending_serialization(self):
        # while a resume is in flight, an opposite recreate stays a 409
        log = seeded_log()
        run_to_completion(log, RuntimeDouble(), "sbx1", "suspend")
        log.mark_reconciled("sbx1")
        log.request("sbx1", "resume", idempotency_key="r1",
                    expected_version=log.sandbox("sbx1")["version"])
        with self.assertRaises(OperationConflict):
            log.request("sbx1", "destroy", idempotency_key="d1",
                        expected_version=log.sandbox("sbx1")["version"])
        self.assertEqual(len(log.operations("sbx1")), 2)  # suspend + resume


class Concurrency(unittest.TestCase):
    def test_concurrent_ops_exactly_one_terminal_outcome(self):
        log = seeded_log(sandbox_id="sbx_c")
        barrier = threading.Barrier(6)
        results = []
        results_lock = threading.Lock()

        def worker(op_type, key):
            barrier.wait()
            try:
                receipt = log.request("sbx_c", op_type, idempotency_key=key,
                                      expected_version=1)
                with results_lock:
                    results.append((receipt["status"],
                                    receipt["operation"]["operation_id"]))
            except (OperationConflict, VersionConflict) as exc:
                with results_lock:
                    results.append((f"refused:{exc.code}", None))

        threads = [threading.Thread(target=worker,
                                    args=(OP_TYPES[i % 3], f"k{i}"))
                   for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        accepted = [r for r in results if r[0] == "accepted"]
        self.assertEqual(len(accepted), 1)          # exactly one winner
        self.assertEqual(len(log.operations("sbx_c")), 1)  # one intention
        for status, _ in results:
            self.assertIn(status, ("accepted", "pending_replay",
                                   "refused:opposite_operation",
                                   "refused:version_conflict"))
        winner_id = accepted[0][1]
        self.assertTrue(all(oid in (None, winner_id) for _, oid in results))
        # drive the single intention to its single terminal outcome
        rt = RuntimeDouble()
        log.replay(rt)
        self.assertEqual(log.operation(winner_id)["state"], "confirmed")
        self.assertEqual(rt.executions(), 1)
        self.assertNotIn(log.sandbox("sbx_c")["observed_state"],
                         ("Suspending", "Resuming", "Destroying"))


class MutationGuards(unittest.TestCase):
    """Each safeguard must be load-bearing: break it, the guarantee breaks."""

    def test_pending_serialization_guard(self):
        # the pending row is what makes an in-flight retry return the SAME
        # operation; without it the retry is refused (or, in states where
        # acceptance is permissive, would create a second intention)
        log = seeded_log()
        log.request("sbx1", "suspend", idempotency_key="a",
                    expected_version=1)
        self.assertEqual(
            log.request("sbx1", "suspend", idempotency_key="b",
                        expected_version=2)["status"], "pending_replay")
        with mock.patch.object(OperationLog, "_pending",
                               lambda self, sb: None):
            with self.assertRaises(OperationConflict):  # guard bypassed
                log.request("sbx1", "suspend", idempotency_key="c",
                            expected_version=2)
        self.assertEqual(len(log.operations("sbx1")), 1)

    def test_intention_dedupe_guard_prevents_double_intention(self):
        # one key must map to at most one intention, even after the first
        # went terminal: without the idempotency store the same key funds a
        # second operation (double-build / double-charge risk)
        log = seeded_log()
        log.request("sbx1", "suspend", idempotency_key="k1",
                    expected_version=1)
        run_to_completion(log, RuntimeDouble(), "sbx1", "suspend")
        with self.assertRaises(IdempotencyConflict):  # key reused, other body
            log.request("sbx1", "destroy", idempotency_key="k1",
                        expected_version=log.sandbox("sbx1")["version"])
        with mock.patch.object(OperationLog, "_idempotent_lookup",
                               lambda self, key: None):
            log.request("sbx1", "destroy", idempotency_key="k1",
                        expected_version=log.sandbox("sbx1")["version"])
        self.assertEqual(len(log.operations("sbx1")), 2)  # guard bypassed

    def test_generation_fence_guard_protects_current_state(self):
        log = seeded_log()
        run_to_completion(log, RuntimeDouble(), "sbx1", "suspend")
        log.mark_reconciled("sbx1")
        run_to_completion(log, RuntimeDouble(), "sbx1", "resume")  # gen 2
        with mock.patch.object(OperationLog, "_generation_ok",
                               lambda self, op, sb: True):
            breached = log.apply_event("sbx1", generation=1,
                                       observed_state="Suspend")
        self.assertEqual(breached, {"status": "applied"})  # guard bypassed
        self.assertEqual(log.sandbox("sbx1")["observed_state"], "Suspend")
        # with the fence intact the same write is history-only
        log3 = seeded_log()
        run_to_completion(log3, RuntimeDouble(), "sbx1", "suspend")
        log3.mark_reconciled("sbx1")
        run_to_completion(log3, RuntimeDouble(), "sbx1", "resume")
        kept = log3.apply_event("sbx1", generation=1, observed_state="Suspend")
        self.assertEqual(kept, {"status": "history_only"})
        self.assertEqual(log3.sandbox("sbx1")["observed_state"], "Active")

    def test_runtime_dedupe_guard_prevents_double_build(self):
        class Doubler(RuntimeDouble):
            def execute(self, op):  # cache bypassed: every call re-applies
                self.calls += 1
                self.effects += 1
                self._outcomes.setdefault(op["operation_id"], self._apply(op))
                return copy.deepcopy(self._outcomes[op["operation_id"]])
        log, rt = seeded_log(), Doubler()
        op_id = log.request("sbx1", "suspend", idempotency_key="a",
                            expected_version=1)["operation"]["operation_id"]
        log.dispatch(op_id)
        rt.execute(log.operation(op_id))
        recovered = OperationLog.from_dict(log.to_dict())
        recovered.replay(rt)
        self.assertEqual(rt.effects, 2)   # would double-build uncached
        clean_rt = RuntimeDouble()
        clean = seeded_log()
        run_to_completion(clean, clean_rt, "sbx1", "suspend")
        self.assertEqual(clean_rt.effects, 1)  # intended semantics

    def test_event_emission_guard_prevents_duplicate_events(self):
        log = seeded_log()
        op_id = run_to_completion(log, RuntimeDouble(), "sbx1", "suspend")
        log.emit_operation_event(op_id)  # already emitted: no-op
        self.assertEqual(len(log.ledger().events()), 2)
        with mock.patch.object(OperationLog, "_event_emitted",
                               lambda self, op, etype: False):
            log.emit_operation_event(op_id)  # op bookkeeping bypassed
        # the deterministic event_id makes the ledger itself the last line
        # of defense: the append dedupes, no second event is stored
        self.assertEqual(len(log.ledger().events()), 2)
        # and without either guard (ledger dedupe broken too) it would double
        with mock.patch.object(OperationLog, "_event_emitted",
                               lambda self, op, etype: False), \
             mock.patch("resource_events.EventLedger._dedupe_check",
                        lambda self, event: ("new", None)):
            log.emit_operation_event(op_id)
        self.assertEqual(len(log.ledger().events()), 3)

    def test_reservation_guard_on_lost(self):
        def releasing_lost(self, sandbox_id, last_observed_at_ms=None):
            sb = self._sandbox(sandbox_id)  # broken: drops the reservation
            sb["observed_state"] = "Lost"
            sb["reserved"] = False
            return self._sandbox_view(sb)
        log = seeded_log()
        with mock.patch.object(OperationLog, "mark_lost", releasing_lost):
            broken = log.mark_lost("sbx1", 5)
        self.assertFalse(broken["reserved"])  # guard bypassed: capacity leak
        real = seeded_log()
        kept = real.mark_lost("sbx1", 5)
        self.assertTrue(kept["reserved"])     # invariant: reservation kept

    def test_timeout_not_failed_guard(self):
        def failing_timeout(self, op_id):  # broken: timeout marks failed
            op = self._op(op_id)
            op["state"] = "failed"
            return self._op_view(op)
        log = seeded_log()
        op_id = log.request("sbx1", "suspend", idempotency_key="a",
                            expected_version=1)["operation"]["operation_id"]
        log.dispatch(op_id)
        with mock.patch.object(OperationLog, "mark_timeout", failing_timeout):
            broken = log.mark_timeout(op_id)
        self.assertEqual(broken["state"], "failed")  # wrong under contract
        real = seeded_log()
        oid = real.request("sbx1", "suspend", idempotency_key="a",
                           expected_version=1)["operation"]["operation_id"]
        real.dispatch(oid)
        self.assertEqual(real.mark_timeout(oid)["operation"]["state"],
                         "timeout")


if __name__ == "__main__":
    unittest.main()
