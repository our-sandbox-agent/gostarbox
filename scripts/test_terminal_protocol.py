"""Guard tests: each verifier rule must reject a contract that breaks it (CONTRIBUTING)."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from verify_terminal_protocol import check_contract  # noqa: E402

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "docs/contracts/terminal-protocol.json").read_text())


def mutated(*editors):
    doc = copy.deepcopy(CONTRACT)
    for edit in editors:
        edit(doc)
    return check_contract(doc)


def find_frame(doc, name):
    return next(f for f in doc["frames"] if f["name"] == name)


class ShippedContract(unittest.TestCase):
    def test_shipped_contract_passes(self):
        self.assertEqual(check_contract(CONTRACT), [])


class RejectsBrokenContracts(unittest.TestCase):
    def test_duplicate_frame_name_rejected(self):
        def edit(doc):
            doc["frames"].append(copy.deepcopy(find_frame(doc, "ping")))
        self.assertTrue(any("duplicate frame name" in m for m in mutated(edit)))

    def test_missing_required_field_rejected(self):
        def edit(doc):
            find_frame(doc, "hello")["required_fields"].remove("token")
        self.assertTrue(any("required_fields must be exactly" in m for m in mutated(edit)))

    def test_extra_frame_type_rejected(self):
        def edit(doc):
            doc["frames"].append({"name": "workflow", "direction": "both",
                                  "purpose": "a second protocol",
                                  "required_fields": [], "optional_fields": []})
        self.assertTrue(any("closed frame set" in m for m in mutated(edit)))

    def test_unknown_error_code_rejected(self):
        def edit(doc):
            doc["error_codes"].append({"code": "terminal_on_fire", "carrier": "bye.code",
                                       "fatal": True, "client_action": "run"})
        self.assertTrue(any("outside the closed set" in m for m in mutated(edit)))

    def test_missing_error_code_rejected(self):
        def edit(doc):
            doc["error_codes"] = [e for e in doc["error_codes"]
                                  if e["code"] != "rate_limited"]
        self.assertTrue(any("rate_limited" in m and "missing" in m for m in mutated(edit)))

    def test_unlinked_ticket_reference_rejected(self):
        def edit(doc):
            doc["semantics"]["reconnect"]["screen_replay"] += " (see #99)"
        self.assertTrue(any("no integer entry" in m for m in mutated(edit)))

    def test_non_integer_ticket_rejected(self):
        def edit(doc):
            doc["related_tickets"][0]["ticket"] = "13"
        self.assertTrue(any("positive integer ticket" in m for m in mutated(edit)))

    def test_required_ticket_link_removed_rejected(self):
        def edit(doc):
            doc["related_tickets"] = [t for t in doc["related_tickets"]
                                      if t["ticket"] != 19]
        self.assertTrue(any("must be linked" in m for m in mutated(edit)))

    def test_size_bounds_rejected(self):
        def edit(doc):
            doc["semantics"]["size_policy"]["bounds"]["cols"]["max"] = 4096
        self.assertTrue(any("must stay within 1..500" in m for m in mutated(edit)))

    def test_size_policy_flipped_rejected(self):
        def edit(doc):
            doc["semantics"]["size_policy"]["multi_window"]["policy"] = "first_window_wins"
        self.assertTrue(any("last_resize_wins" in m for m in mutated(edit)))

    def test_backpressure_unbounded_rejected(self):
        def edit(doc):
            doc["backpressure"]["buffer"]["bounded"] = False
        self.assertTrue(any("never unbounded" in m for m in mutated(edit)))

    def test_overflow_notice_removed_rejected(self):
        def edit(doc):
            doc["backpressure"]["on_overflow"]["actions"] = [
                a for a in doc["backpressure"]["on_overflow"]["actions"]
                if "output_limit_exceeded" not in a]
        self.assertTrue(any("output_limit_exceeded notice" in m for m in mutated(edit)))

    def test_ping_as_user_activity_rejected(self):
        def edit(doc):
            doc["activity_classification"]["classes"]["user_input"]["frames"] = ["input", "ping"]
        self.assertTrue(any("must be exactly ['input']" in m for m in mutated(edit)))

    def test_liveness_counted_as_activity_rejected(self):
        def edit(doc):
            doc["activity_classification"]["classes"]["liveness"]["counts_as_user_activity"] = True
        self.assertTrue(any("counts_as_user_activity false" in m for m in mutated(edit)))

    def test_output_byte_counter_removed_rejected(self):
        def edit(doc):
            del doc["activity_classification"]["counters"]["output_bytes"]
        self.assertTrue(any("counted separately" in m for m in mutated(edit)))

    def test_bye_after_close_rejected(self):
        def edit(doc):
            doc["lifecycle_binding"]["on_stop_or_destroy"]["rule"] = (
                "when the sandbox stops the server closes the WebSocket and may have sent bye earlier")
        self.assertTrue(any("bye-then-close" in m for m in mutated(edit)))

    def test_destroy_reconnect_code_rejected(self):
        def edit(doc):
            doc["lifecycle_binding"]["reconnect_after_destroy"]["code"] = "auth_failed"
        self.assertTrue(any("sandbox_not_found" in m for m in mutated(edit)))

    def test_wrong_generation_guard_removed_rejected(self):
        def edit(doc):
            del doc["lifecycle_binding"]["wrong_generation_guard"]
        self.assertTrue(any("wrong_generation guard" in m for m in mutated(edit)))

    def test_suspend_not_covered_rejected(self):
        def edit(doc):
            doc["lifecycle_binding"]["not_active"]["note"] = "any non-running state"
        self.assertTrue(any("including Suspend" in m for m in mutated(edit)))

    def test_detach_kills_tmux_rejected(self):
        def edit(doc):
            doc["lifecycle_binding"]["detach"]["statement"] = (
                "detach kills the tmux session to save memory")
        self.assertTrue(any("never kills" in m for m in mutated(edit)))

    def test_recording_capability_rejected(self):
        def edit(doc):
            doc["semantics"]["scrollback_buffer"] = {"frames_kept": 1000}
        self.assertTrue(any("forbidden terminal-content capture feature" in m
                            for m in mutated(edit)))

    def test_recording_frame_rejected(self):
        def edit(doc):
            doc["frames"].append({"name": "record", "direction": "server_to_client",
                                  "purpose": "keep terminal output",
                                  "required_fields": ["data"], "optional_fields": []})
        self.assertTrue(any("forbidden terminal-content capture feature" in m
                            for m in mutated(edit)))

    def test_backend_decision_closed_rejected(self):
        def edit(doc):
            doc["open_decisions"][0]["status"] = "DECIDED: ttyd"
        self.assertTrue(any("explicitly OPEN" in m for m in mutated(edit)))


if __name__ == "__main__":
    unittest.main()
