"""Contract tests: the required #76 flow plus mutation-style guards against the double.

Flow coverage (issue #76 acceptance): create -> inspect -> suspend (202 -> poll ->
confirmed) -> resume (credentials_required without a key, success with one) ->
destroy; idempotency; version fencing; auth; unknown ids; capacity; observed-state
honesty; API restart via snapshot/restore.

Guard coverage: each test breaks the double at a deliberate mutation point and
asserts the exact invariant the flow tests rely on flips — i.e. the suite is not
vacuous against a buggy double (CONTRIBUTING mutate-and-fail idiom).
"""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from control_plane_double import ControlPlaneDouble  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}
CREATE_BODY = {"agent": "claude",
               "resources": {"milli_cpu": 1000, "memory_bytes": 1 << 30,
                             "volume_bytes": 10 << 30}}


def req(double, method, path, body=None, key=None, **extra):
    headers = dict(AUTH)
    if key:
        headers["Idempotency-Key"] = key
    headers.update(extra)
    return double.request(method, path, headers, body)


def create(double, body=CREATE_BODY, key="c1"):
    return req(double, "POST", "/v1/sandboxes", body, key)


def view(double, sid):
    return double.request("GET", f"/v1/sandboxes/{sid}", AUTH, {})[1]


def version_of(double, sid):
    return view(double, sid)["version"]


def state(double, sid, body, key):
    return req(double, "POST", f"/v1/sandboxes/{sid}/state", body, key)


def destroy(double, sid, expected_version, key):
    return req(double, "DELETE", f"/v1/sandboxes/{sid}",
               {"expected_version": expected_version,
                "confirm_scope": "workspace_and_home_volumes"}, key)


def make_active(double, key="c1", body=CREATE_BODY):
    """create + confirmed create operation -> observed Active."""
    status, resp = create(double, body=body, key=key)
    assert status == 202, resp
    double.confirm(resp["operation"]["operation_id"])
    return resp["sandbox_id"]


def suspend_confirmed(double, sid, key="s1"):
    status, body = state(double, sid,
                         {"state": "Suspend", "expected_version": version_of(double, sid),
                          "suspend_mode": "cold"}, key)
    assert status == 202, body
    double.confirm(body["operation"]["operation_id"])
    return body["operation"]["operation_id"]


class RequiredFlow(unittest.TestCase):
    def test_create_inspect_suspend_resume_destroy(self):
        d = ControlPlaneDouble()
        status, body = create(d, key="c1")
        self.assertEqual(status, 202)
        sid, op = body["sandbox_id"], body["operation"]
        self.assertIsNone(op["expected_version"])  # create carries no fence
        self.assertIn(op["state"], {"pending", "running"})
        v = view(d, sid)
        self.assertEqual(v["observed_state"], "Creating")  # never optimistic Active
        self.assertEqual(v["desired_state"], "Active")
        self.assertEqual(v["generation"], 1)
        self.assertEqual(v["pending_operation"]["operation_id"], op["operation_id"])
        self.assertIsNone(v["session"])

        d.advance(op["operation_id"])
        self.assertEqual(d.request("GET", f"/v1/operations/{op['operation_id']}",
                                   AUTH, {})[1]["state"], "running")
        d.confirm(op["operation_id"])
        v = view(d, sid)
        self.assertEqual(v["observed_state"], "Active")
        self.assertEqual(v["session"], {"id": f"claude-session-{sid}",
                                        "cwd": "/workspace"})
        self.assertIsNotNone(v["last_confirmed_at"])
        self.assertIsNone(v["pending_operation"])

        status, body = state(d, sid, {"state": "Suspend",
                                      "expected_version": version_of(d, sid),
                                      "suspend_mode": "cold"}, "s1")
        self.assertEqual(status, 202)
        sop = body["operation"]
        v = view(d, sid)
        self.assertEqual(v["observed_state"], "Suspending")  # 202 != confirmed stop
        self.assertEqual(v["desired_state"], "Suspend")
        self.assertEqual(v["pending_operation"]["operation_id"], sop["operation_id"])
        d.confirm(sop["operation_id"])
        self.assertEqual(view(d, sid)["observed_state"], "Suspend")

        # resume without a credential: 409, no operation, stays Suspend
        ops = len(d.operations)
        status, body = state(d, sid, {"state": "Active",
                                      "expected_version": version_of(d, sid)}, "r1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "credentials_required")
        self.assertEqual(len(d.operations), ops)
        v = view(d, sid)
        self.assertEqual(v["observed_state"], "Suspend")
        self.assertIsNone(v["pending_operation"])
        self.assertEqual(v["generation"], 1)

        # resume with the credential, same Idempotency-Key and body (credential
        # excluded from the body comparison)
        status, body = state(d, sid, {"state": "Active",
                                      "expected_version": version_of(d, sid),
                                      "credential": {"api_key": "sk-test"}}, "r1")
        self.assertEqual(status, 202)
        rop = body["operation"]
        v = view(d, sid)
        self.assertEqual(v["observed_state"], "Resuming")
        self.assertEqual(v["generation"], 2)  # cold resume -> new generation
        d.confirm(rop["operation_id"])
        v = view(d, sid)
        self.assertEqual(v["observed_state"], "Active")
        self.assertEqual(v["generation"], 2)

        status, body = destroy(d, sid, version_of(d, sid), "d1")
        self.assertEqual(status, 202)
        dop = body["operation"]
        self.assertEqual(view(d, sid)["observed_state"], "Destroying")
        d.confirm(dop["operation_id"])
        v = view(d, sid)
        self.assertEqual(v["observed_state"], "Destroyed")
        self.assertIsNone(v["session"])

        # repeat destroy after Destroyed: existing operation, not a second one
        status, body = destroy(d, sid, version_of(d, sid), "d2")
        self.assertEqual(status, 202)
        self.assertEqual(body["operation"]["operation_id"], dop["operation_id"])
        self.assertEqual(len(d.operations), 4)

    def test_idempotency_same_key_same_body_no_double_create(self):
        d = ControlPlaneDouble()
        status1, b1 = create(d, key="k")
        status2, b2 = create(d, key="k")
        self.assertEqual((status1, status2), (202, 202))
        self.assertEqual(b1["operation"]["operation_id"],
                         b2["operation"]["operation_id"])
        self.assertEqual(b1["sandbox_id"], b2["sandbox_id"])
        _, listing = d.request("GET", "/v1/sandboxes", AUTH, {})
        self.assertEqual(len(listing["sandboxes"]), 1)
        d.confirm(b1["operation"]["operation_id"])
        status3, b3 = create(d, key="k")  # replay after terminal op
        self.assertEqual(status3, 202)
        self.assertEqual(b3["operation"]["operation_id"],
                         b1["operation"]["operation_id"])
        self.assertEqual(b3["operation"]["state"], "succeeded")

    def test_idempotency_same_key_different_body_409(self):
        d = ControlPlaneDouble()
        create(d, key="k")
        other = dict(CREATE_BODY, resources=dict(CREATE_BODY["resources"],
                                                 milli_cpu=2000))
        status, body = create(d, body=other, key="k")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "body_conflict")
        _, listing = d.request("GET", "/v1/sandboxes", AUTH, {})
        self.assertEqual(len(listing["sandboxes"]), 1)

    def test_expected_version_mismatch_409(self):
        d = ControlPlaneDouble()
        sid = make_active(d)
        stale = version_of(d, sid) - 1
        status, body = state(d, sid, {"state": "Suspend",
                                      "expected_version": stale}, "s1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "version_conflict")
        status, body = destroy(d, sid, stale, "d1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "version_conflict")
        # the correct version still works afterwards
        status, _ = state(d, sid, {"state": "Suspend",
                                   "expected_version": version_of(d, sid)}, "s2")
        self.assertEqual(status, 202)

    def test_missing_idempotency_key_422(self):
        d = ControlPlaneDouble()
        sid = make_active(d)
        status, body = d.request("POST", "/v1/sandboxes", AUTH, CREATE_BODY)
        self.assertEqual((status, body["error"]["code"]), (422, "invalid"))
        status, body = d.request("POST", f"/v1/sandboxes/{sid}/state", AUTH,
                                 {"state": "Suspend", "expected_version": 2})
        self.assertEqual((status, body["error"]["code"]), (422, "invalid"))
        status, body = d.request("DELETE", f"/v1/sandboxes/{sid}", AUTH,
                                 {"expected_version": 2,
                                  "confirm_scope": "workspace_and_home_volumes"})
        self.assertEqual((status, body["error"]["code"]), (422, "invalid"))

    def test_no_auth_401_on_every_endpoint(self):
        d = ControlPlaneDouble()
        sid = make_active(d)
        op_id = next(iter(d.operations))
        probes = [
            ("POST", "/v1/sandboxes", CREATE_BODY),
            ("GET", "/v1/sandboxes", None),
            ("GET", f"/v1/sandboxes/{sid}", None),
            ("POST", f"/v1/sandboxes/{sid}/state", {"state": "Suspend",
                                                     "expected_version": 2}),
            ("DELETE", f"/v1/sandboxes/{sid}", {"expected_version": 2,
                                                 "confirm_scope": "x"}),
            ("GET", f"/v1/operations/{op_id}", None),
            ("POST", f"/v1/sandboxes/{sid}/terminal-ticket", {}),
        ]
        for method, path, body in probes:
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                status, resp = d.request(method, path, headers, body)
                self.assertEqual(status, 401, f"{method} {path}")
                self.assertEqual(resp["error"]["code"], "unauthorized")

    def test_unknown_ids_404(self):
        d = ControlPlaneDouble()
        for method, path, body in [
                ("GET", "/v1/sandboxes/sbx_999999", None),
                ("POST", "/v1/sandboxes/sbx_999999/state",
                 {"state": "Active", "expected_version": 1}),
                ("DELETE", "/v1/sandboxes/sbx_999999",
                 {"expected_version": 1, "confirm_scope": "workspace_and_home_volumes"}),
                ("POST", "/v1/sandboxes/sbx_999999/terminal-ticket", {}),
                ("GET", "/v1/operations/op_999999", None)]:
            status, resp = req(d, method, path, body, key="k")
            self.assertEqual(status, 404, f"{method} {path}")
            self.assertEqual(resp["error"]["code"], "not_found")

    def test_capacity_exceeded_429(self):
        cap = {"milli_cpu": 2000, "memory_bytes": 2 << 30, "volume_bytes": 20 << 30}
        d = ControlPlaneDouble(capacity=cap)
        status, _ = create(d, dict(CREATE_BODY, resources={
            "milli_cpu": 1500, "memory_bytes": 1 << 30, "volume_bytes": 1 << 30}), "a")
        self.assertEqual(status, 202)
        status, body = create(d, dict(CREATE_BODY, resources={
            "milli_cpu": 1500, "memory_bytes": 1 << 30, "volume_bytes": 1 << 30}), "b")
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "capacity_exceeded")
        self.assertEqual(len(d.operations), 1)
        _, listing = d.request("GET", "/v1/sandboxes", AUTH, {})
        self.assertEqual(len(listing["sandboxes"]), 1)

        # resume-side admission: compute freed by a confirmed suspend is taken
        small = dict(CREATE_BODY, resources={"milli_cpu": 1500,
                                             "memory_bytes": 1 << 30,
                                             "volume_bytes": 1 << 30})
        sid = make_active(d, key="a", body=small)
        suspend_confirmed(d, sid)
        status, _ = create(d, small, "b2")
        self.assertEqual(status, 202)
        status, body = state(d, sid, {"state": "Active",
                                      "expected_version": version_of(d, sid),
                                      "credential": {"api_key": "sk-test"}}, "r")
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "capacity_exceeded")

    def test_unknown_runtime_state_pending_or_lost_never_active(self):
        d = ControlPlaneDouble()
        _, body = create(d, key="c1")
        sid = body["sandbox_id"]
        self.assertEqual(view(d, sid)["observed_state"], "Creating")
        d.expire_lease(sid)
        v = view(d, sid)
        self.assertEqual(v["observed_state"], "Lost")  # unknown -> Lost, not Active
        status, body = state(d, sid, {"state": "Active",
                                      "expected_version": version_of(d, sid),
                                      "credential": {"api_key": "sk-test"}}, "r")
        self.assertEqual(status, 409)  # fencing/reconciliation first
        status, body = destroy(d, sid, version_of(d, sid), "d")
        self.assertEqual(status, 409)
        d.mark_reconciled(sid)
        status, body = state(d, sid, {"state": "Active",
                                      "expected_version": version_of(d, sid),
                                      "credential": {"api_key": "sk-test"}}, "r")
        self.assertEqual(status, 202)
        self.assertEqual(view(d, sid)["observed_state"], "Resuming")

        # a requested state change stays unconfirmed until Runner evidence
        sid2 = make_active(d, key="c2")
        state(d, sid2, {"state": "Suspend",
                        "expected_version": version_of(d, sid2)}, "s")
        self.assertEqual(view(d, sid2)["observed_state"], "Suspending")

    def test_api_restart_snapshot_restore(self):
        d = ControlPlaneDouble()
        _, body = create(d, key="c1")
        sid = body["sandbox_id"]
        d.confirm(body["operation"]["operation_id"])
        suspend_confirmed(d, sid)
        before = view(d, sid)
        op1 = next(iter(d.operations))

        blob = json.loads(json.dumps(d.snapshot()))  # must be plain JSON
        d2 = ControlPlaneDouble()
        d2.restore(blob)
        after = view(d2, sid)
        self.assertEqual(after["observed_state"], before["observed_state"])
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["generation"], before["generation"])
        status, op_view = d2.request("GET", f"/v1/operations/{op1}", AUTH, {})
        self.assertEqual(status, 200)
        self.assertEqual(op_view["state"], "succeeded")
        # idempotency survives the restart: replay does not double-create
        status, replay = create(d2, key="c1")
        self.assertEqual(status, 202)
        self.assertEqual(replay["sandbox_id"], sid)
        self.assertEqual(replay["operation"]["operation_id"], op1)
        _, listing = d2.request("GET", "/v1/sandboxes", AUTH, {})
        self.assertEqual(len(listing["sandboxes"]), 1)

    def test_credential_never_in_operation_payload_or_snapshot(self):
        d = ControlPlaneDouble()
        sid = make_active(d)
        suspend_confirmed(d, sid)
        secret = "sk-SECRET-123"
        status, body = state(d, sid, {"state": "Active",
                                      "expected_version": version_of(d, sid),
                                      "credential": {"api_key": secret}}, "r")
        self.assertEqual(status, 202)
        op_json = json.dumps(body["operation"])
        self.assertNotIn(secret, op_json)
        self.assertNotIn("credential", body["operation"])
        self.assertNotIn(secret, json.dumps(view(d, sid)))
        self.assertNotIn(secret, json.dumps(d.snapshot()))
        d.confirm(body["operation"]["operation_id"])
        self.assertNotIn(secret, json.dumps(d.snapshot()))

    def test_opposite_operation_and_resuming_lock(self):
        d = ControlPlaneDouble()
        sid = make_active(d)
        status, body = state(d, sid, {"state": "Suspend",
                                      "expected_version": version_of(d, sid)}, "s1")
        self.assertEqual(status, 202)
        suspend_op = body["operation"]["operation_id"]
        status, body = state(d, sid, {"state": "Active",
                                      "expected_version": version_of(d, sid)}, "x")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "opposite_operation")
        # replay of the same target returns the same pending operation
        status, body = state(d, sid, {"state": "Suspend",
                                      "expected_version": version_of(d, sid)}, "s2")
        self.assertEqual(status, 202)
        self.assertEqual(body["operation"]["operation_id"], suspend_op)
        self.assertEqual(len(d.operations), 2)
        d.confirm(suspend_op)
        status, body = state(d, sid, {"state": "Active",
                                      "expected_version": version_of(d, sid),
                                      "credential": {"api_key": "sk-test"}}, "r")
        self.assertEqual(status, 202)
        self.assertEqual(view(d, sid)["observed_state"], "Resuming")
        # while Resuming: any change or destroy is refused
        status, _ = state(d, sid, {"state": "Suspend",
                                   "expected_version": version_of(d, sid)}, "z")
        self.assertEqual(status, 409)
        status, _ = destroy(d, sid, version_of(d, sid), "d")
        self.assertEqual(status, 409)

    def test_warm_suspend_mode_422(self):
        d = ControlPlaneDouble()
        sid = make_active(d)
        status, body = state(d, sid, {"state": "Suspend",
                                      "expected_version": version_of(d, sid),
                                      "suspend_mode": "warm"}, "s1")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "unsupported_suspend_mode")
        self.assertEqual(len(d.operations), 1)
        self.assertEqual(view(d, sid)["observed_state"], "Active")

    def test_error_state_requires_reconciliation(self):
        d = ControlPlaneDouble()
        sid = make_active(d)
        status, body = state(d, sid, {"state": "Suspend",
                                      "expected_version": version_of(d, sid)}, "s1")
        failed = d.confirm(body["operation"]["operation_id"],
                           error={"code": "recovery_retry_exhausted",
                                  "message": "bounded retries exhausted"})
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error"]["code"], "recovery_retry_exhausted")
        v = view(d, sid)
        self.assertEqual(v["observed_state"], "Error")
        self.assertIsNotNone(v["last_confirmed_state"])
        self.assertEqual(v["error"]["code"], "recovery_retry_exhausted")
        status, _ = state(d, sid, {"state": "Active",
                                   "expected_version": version_of(d, sid),
                                   "credential": {"api_key": "sk-test"}}, "r")
        self.assertEqual(status, 409)  # error-before-reconcile
        d.mark_reconciled(sid)
        status, body = state(d, sid, {"state": "Active",
                                      "expected_version": version_of(d, sid),
                                      "credential": {"api_key": "sk-test"}}, "r2")
        self.assertEqual(status, 202)
        self.assertEqual(view(d, sid)["observed_state"], "Resuming")

    def test_terminal_ticket(self):
        d = ControlPlaneDouble()
        sid = make_active(d)
        status, body = req(d, "POST", f"/v1/sandboxes/{sid}/terminal-ticket", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["ticket"].startswith("tt_"))
        self.assertEqual(body["sandbox_id"], sid)
        self.assertEqual(body["generation"], view(d, sid)["generation"])
        status, _ = req(d, "POST", "/v1/sandboxes/sbx_999999/terminal-ticket", {})
        self.assertEqual(status, 404)
        destroy(d, sid, version_of(d, sid), "d")
        d.confirm(next(iter(op for op in d.operations.values()
                            if op["type"] == "destroy"))["operation_id"])
        status, _ = req(d, "POST", f"/v1/sandboxes/{sid}/terminal-ticket", {})
        self.assertEqual(status, 404)

    def test_same_target_already_achieved_200(self):
        d = ControlPlaneDouble()
        sid = make_active(d)
        ops = len(d.operations)
        status, body = state(d, sid, {"state": "Active",
                                      "expected_version": version_of(d, sid)}, "a")
        self.assertEqual(status, 200)
        self.assertEqual(body["sandbox"]["observed_state"], "Active")
        self.assertEqual(len(d.operations), ops)
        suspend_confirmed(d, sid)
        status, _ = state(d, sid, {"state": "Suspend",
                                   "expected_version": version_of(d, sid)}, "b")
        self.assertEqual(status, 200)


# --------------------------------------------------------------------- guards
# Each mutant subclass breaks one safeguard of the double at a deliberate
# mutation point; each guard proves the flow-test invariant flips on the mutant
# and holds on the real double (non-vacuity, CONTRIBUTING mutate-and-fail rule).

class OptimisticCreateMutant(ControlPlaneDouble):
    def _apply_create_observation(self, sb):
        sb["observed_state"] = "Active"  # the dishonesty the ADR forbids


class AuthBypassMutant(ControlPlaneDouble):
    def _authorized(self, headers):
        return True


class BlindFingerprintMutant(ControlPlaneDouble):
    def _fingerprint(self, body):
        return "constant"  # same key + different body would replay silently


class NoDedupMutant(ControlPlaneDouble):
    def _idempotent_lookup(self, key):
        return None  # replays double-create


class UnfencedVersionMutant(ControlPlaneDouble):
    def _version_ok(self, sb, expected):
        return True  # ignores expected_version


class LeakyCredentialMutant(ControlPlaneDouble):
    def _store_credential(self, sb, op, credential):
        op["target"] = dict(op["target"], credential=credential)


class UnfencedLostMutant(ControlPlaneDouble):
    def _state_refusal(self, sb, target):
        if sb["observed_state"] == "Lost":
            return None  # accepts changes before fencing/reconciliation
        return super()._state_refusal(sb, target)


class NoCapacityAccountingMutant(ControlPlaneDouble):
    def _reserved_compute(self):
        return {"milli_cpu": 0, "memory_bytes": 0}


class MutationGuards(unittest.TestCase):
    def test_guard_optimistic_active_breaks_honesty_probe(self):
        real = ControlPlaneDouble()
        _, b = create(real)
        self.assertEqual(view(real, b["sandbox_id"])["observed_state"], "Creating")
        broken = OptimisticCreateMutant()
        _, b = create(broken)
        self.assertEqual(view(broken, b["sandbox_id"])["observed_state"], "Active")

    def test_guard_auth_bypass_breaks_401_probe(self):
        real = ControlPlaneDouble()
        status, _ = real.request("GET", "/v1/sandboxes", {}, {})
        self.assertEqual(status, 401)
        broken = AuthBypassMutant()
        status, _ = broken.request("GET", "/v1/sandboxes", {}, {})
        self.assertNotEqual(status, 401)

    def test_guard_blind_fingerprint_breaks_body_conflict_probe(self):
        other = dict(CREATE_BODY, resources=dict(CREATE_BODY["resources"],
                                                 milli_cpu=2000))
        real = ControlPlaneDouble()
        create(real, key="k")
        status, _ = create(real, body=other, key="k")
        self.assertEqual(status, 409)
        broken = BlindFingerprintMutant()
        create(broken, key="k")
        status, _ = create(broken, body=other, key="k")
        self.assertNotEqual(status, 409)

    def test_guard_no_dedup_breaks_single_sandbox_probe(self):
        real = ControlPlaneDouble()
        create(real, key="k")
        create(real, key="k")
        self.assertEqual(len(real.sandboxes), 1)
        broken = NoDedupMutant()
        create(broken, key="k")
        create(broken, key="k")
        self.assertEqual(len(broken.sandboxes), 2)

    def test_guard_unfenced_version_breaks_409_probe(self):
        real = ControlPlaneDouble()
        sid = make_active(real)
        status, _ = state(real, sid, {"state": "Suspend",
                                      "expected_version": 999}, "s")
        self.assertEqual(status, 409)
        broken = UnfencedVersionMutant()
        sid = make_active(broken)
        status, _ = state(broken, sid, {"state": "Suspend",
                                        "expected_version": 999}, "s")
        self.assertNotEqual(status, 409)

    def test_guard_leaky_credential_breaks_no_secret_probe(self):
        secret = "sk-LEAK-123"
        for cls, present in ((ControlPlaneDouble, False),
                             (LeakyCredentialMutant, True)):
            d = cls()
            sid = make_active(d)
            suspend_confirmed(d, sid)
            _, body = state(d, sid, {"state": "Active",
                                     "expected_version": version_of(d, sid),
                                     "credential": {"api_key": secret}}, "r")
            self.assertEqual(secret in json.dumps(body["operation"]), present)

    def test_guard_unfenced_lost_breaks_reconcile_probe(self):
        real = ControlPlaneDouble()
        sid = make_active(real)
        real.expire_lease(sid)
        status, _ = state(real, sid, {"state": "Active",
                                      "expected_version": version_of(real, sid),
                                      "credential": {"api_key": "sk"}}, "r")
        self.assertEqual(status, 409)
        broken = UnfencedLostMutant()
        sid = make_active(broken)
        broken.expire_lease(sid)
        status, _ = state(broken, sid, {"state": "Active",
                                        "expected_version": version_of(broken, sid),
                                        "credential": {"api_key": "sk"}}, "r")
        self.assertNotEqual(status, 409)

    def test_guard_no_capacity_accounting_breaks_429_probe(self):
        cap = {"milli_cpu": 2000, "memory_bytes": 2 << 30, "volume_bytes": 20 << 30}
        body = dict(CREATE_BODY, resources={"milli_cpu": 1500,
                                            "memory_bytes": 1 << 30,
                                            "volume_bytes": 1 << 30})
        real = ControlPlaneDouble(capacity=cap)
        create(real, body, "a")
        status, _ = create(real, body, "b")
        self.assertEqual(status, 429)
        broken = NoCapacityAccountingMutant(capacity=cap)
        create(broken, body, "a")
        status, _ = create(broken, body, "b")
        self.assertNotEqual(status, 429)


if __name__ == "__main__":
    unittest.main()
