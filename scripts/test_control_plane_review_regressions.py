"""#129: keep the Python reference and HTTP implementation's fixes aligned."""
import unittest
from test_control_plane_contract import (
    ControlPlaneDouble, CREATE_BODY, create, destroy, make_active,
    req, state, suspend_confirmed, version_of, view,
)


class ReviewRegressions(unittest.TestCase):
    def test_resume_generation(self):
        d = ControlPlaneDouble()
        sid = make_active(d)
        suspend_confirmed(d, sid)
        body = {"state": "Active", "expected_version": version_of(d, sid), "credential": "dummy"}
        code, response = state(d, sid, body, "resume")
        self.assertEqual(code, 202)
        op = response["operation"]
        generation = view(d, sid)["generation"]
        self.assertEqual(op["generation"], generation)
        body["credential"] = "replacement"
        self.assertEqual(state(d, sid, body, "resume")[1]["operation"]["generation"], generation)
        self.assertEqual(req(d, "GET", f"/v1/operations/{op['operation_id']}")[1]["generation"], generation)
        self.assertEqual(d.confirm(op["operation_id"])["result"]["generation"], generation)

    def test_destroy_supersedes_and_fences_old_callbacks(self):
        for creating in [True, False]:
            for error in [None, {"code": "late_error"}]:
                with self.subTest(creating=creating, error=error):
                    d = ControlPlaneDouble()
                    _, c = create(d)
                    sid, old = c["sandbox_id"], c["operation"]["operation_id"]
                    if not creating:
                        d.confirm(old)
                        _, r = state(d, sid, {"state": "Idle", "expected_version": version_of(d, sid)}, "idle")
                        old = r["operation"]["operation_id"]
                    code, r = destroy(d, sid, version_of(d, sid), "destroy")
                    self.assertEqual(code, 202)
                    before = d.snapshot()
                    self.assertEqual(d.confirm(old, error)["state"], "failed")
                    self.assertEqual(d.snapshot(), before)
                    self.assertEqual(d.confirm(r["operation"]["operation_id"])["result"]["observed_state"], "Destroyed")

    def test_stale_generation_owner_and_phase_refused(self):
        for field, value in [("generation", 99), ("pending_operation", None), ("observed_state", "Lost")]:
            for error in [None, {"code": "late_error"}]:
                d = ControlPlaneDouble()
                _, c = create(d)
                d.sandboxes[c["sandbox_id"]][field] = value
                before = d.snapshot()
                with self.assertRaises(RuntimeError):
                    d.confirm(c["operation"]["operation_id"], error)
                self.assertEqual(d.snapshot(), before)

    def test_successful_noop_idempotency_survives_restart(self):
        d = ControlPlaneDouble()
        sid = make_active(d)
        body = {"state": "Active", "expected_version": version_of(d, sid)}
        first = state(d, sid, body, "noop")
        self.assertEqual(first[0], 200)
        self.assertEqual(state(d, sid, {**body, "state": "Suspend"}, "noop")[1]["error"]["code"], "body_conflict")
        suspend_confirmed(d, sid)
        other = ControlPlaneDouble()
        other.restore(d.snapshot())
        self.assertEqual(state(other, sid, body, "noop"), first)

    def test_authenticated_schema_guards(self):
        d = ControlPlaneDouble()
        sid = make_active(d)
        cases = [
            ("DELETE", f"/v1/sandboxes/{sid}", {"expected_version": version_of(d, sid), "confirm_scope": "wrong"}),
            ("POST", "/v1/sandboxes", {**CREATE_BODY, "agent": "other"}),
            ("POST", "/v1/sandboxes", {**CREATE_BODY, "repo_url": "http://example.test"}),
            ("POST", "/v1/sandboxes", {**CREATE_BODY, "runtime_deadline_at": -1}),
            ("POST", f"/v1/sandboxes/{sid}/state", {"state": "Idle", "expected_version": "2"}),
        ]
        for resource in ["milli_cpu", "memory_bytes", "volume_bytes"]:
            for bad in [0, -1, 1.5, "1", None]:
                cases.append(("POST", "/v1/sandboxes", {**CREATE_BODY, "resources": {**CREATE_BODY["resources"], resource: bad}}))
        for method, path, body in cases:
            before = d.snapshot()
            code, response = req(d, method, path, body, "invalid")
            self.assertEqual(code, 422)
            self.assertEqual(response["error"]["code"], "invalid")
            self.assertEqual(d.snapshot(), before)
