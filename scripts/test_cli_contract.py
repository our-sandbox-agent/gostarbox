"""Guard tests: each verifier rule must reject a contract that breaks it (CONTRIBUTING)."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from verify_cli_contract import check_contract  # noqa: E402

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "docs/contracts/cli-surface.json").read_text())


def mutated(*editors):
    doc = copy.deepcopy(CONTRACT)
    for edit in editors:
        edit(doc)
    return check_contract(doc)


def find_command(doc, name):
    return next(c for c in doc["commands"] if c["name"] == name)


class ShippedContract(unittest.TestCase):
    def test_shipped_contract_passes(self):
        self.assertEqual(check_contract(CONTRACT), [])


class RejectsBrokenContracts(unittest.TestCase):
    def test_extra_command_rejected(self):
        def edit(doc):
            doc["commands"].append({"name": "snapshot", "status": "minimal",
                                    "usage": "sandbox snapshot <id>", "purpose": "snapshot",
                                    "control_plane_only": True, "exit_codes": [0]})
        self.assertTrue(any("closed command set" in m for m in mutated(edit)))

    def test_missing_command_rejected(self):
        def edit(doc):
            doc["commands"] = [c for c in doc["commands"] if c["name"] != "destroy"]
        self.assertTrue(any("required command 'destroy'" in m for m in mutated(edit)))

    def test_command_without_usage_rejected(self):
        def edit(doc):
            find_command(doc, "ls")["usage"] = "list sandboxes"
        self.assertTrue(any("usage string starting with 'sandbox '" in m for m in mutated(edit)))

    def test_undocumented_exit_code_rejected(self):
        def edit(doc):
            find_command(doc, "connect")["exit_codes"].append(77)
        self.assertTrue(any("not documented in exit_codes" in m for m in mutated(edit)))

    def test_direct_runner_flag_rejected(self):
        def edit(doc):
            doc["global_flags"].append(
                {"flag": "--direct_runner", "effect": "talk to the Runner directly"})
        self.assertTrue(any("direct_runner" in m and "forbidden" in m
                            for m in mutated(edit)))

    def test_direct_runner_flag_hyphen_spelling_rejected(self):
        def edit(doc):
            doc["global_flags"].append(
                {"flag": "--direct-runner", "effect": "talk to the Runner directly"})
        self.assertTrue(any("direct-runner" in m and "forbidden" in m
                            for m in mutated(edit)))

    def test_runner_direct_flag_rejected(self):
        def edit(doc):
            doc["global_flags"].append(
                {"flag": "--runner-direct", "effect": "talk to the Runner directly"})
        self.assertTrue(any("runner-direct" in m and "forbidden" in m
                            for m in mutated(edit)))

    def test_command_bypassing_control_plane_rejected(self):
        def edit(doc):
            find_command(doc, "exec")["control_plane_only"] = False
        self.assertTrue(any("control_plane_only must be true" in m for m in mutated(edit)))

    def test_repo_rule_detached_from_clone_args_rejected(self):
        def edit(doc):
            find_command(doc, "claude")["repo_validation"] = "any https-looking string is fine"
        self.assertTrue(any("clone_args.py" in m for m in mutated(edit)))

    def test_exec_shell_parsing_rejected(self):
        def edit(doc):
            find_command(doc, "exec")["literal_argv_rule"] = (
                "argv after -- is joined into a shell command string and run via sh -c")
        self.assertTrue(any("never shell-parsed" in m for m in mutated(edit)))

    def test_cp_as_standalone_complete_rejected(self):
        def edit(doc):
            cp = find_command(doc, "cp")
            cp["status"] = "minimal"
            cp["depends_on"] = []
        self.assertTrue(any("defined-interface" in m or "ticket 20" in m
                            for m in mutated(edit)))

    def test_cp_mocking_success_rejected(self):
        def edit(doc):
            find_command(doc, "cp")["pre_implementation_behavior"] = (
                "until #20 lands the command prints a fake success line")
        self.assertTrue(any("never mocks success" in m for m in mutated(edit)))

    def test_unsupported_zero_exit_rejected(self):
        def edit(doc):
            doc["unsupported_commands"][0]["exit_code"] = 0
        self.assertTrue(any("non-zero integer" in m for m in mutated(edit)))

    def test_unsupported_wrong_code_rejected(self):
        def edit(doc):
            doc["unsupported_commands"][0]["exit_code"] = 1
        self.assertTrue(any("exit 64" in m for m in mutated(edit)))

    def test_unsupported_vague_message_rejected(self):
        def edit(doc):
            doc["unsupported_commands"][0]["message_template"] = "not implemented"
        self.assertTrue(any("{command} placeholder" in m for m in mutated(edit)))

    def test_missing_unsupported_entry_rejected(self):
        def edit(doc):
            doc["unsupported_commands"] = [u for u in doc["unsupported_commands"]
                                           if u["name"] != "snapshot"]
        self.assertTrue(any("'snapshot'" in m and "missing" in m for m in mutated(edit)))

    def test_duplicate_exit_code_rejected(self):
        def edit(doc):
            doc["exit_codes"].append(copy.deepcopy(doc["exit_codes"][0]))
        self.assertTrue(any("duplicate exit code" in m for m in mutated(edit)))

    def test_missing_exit_code_rejected(self):
        def edit(doc):
            doc["exit_codes"] = [e for e in doc["exit_codes"] if e["code"] != 75]
        self.assertTrue(any("exit code 75" in m and "missing" in m for m in mutated(edit)))

    def test_exit_zero_without_detach_note_rejected(self):
        def edit(doc):
            doc["exit_codes"][0]["meaning"] = "success"
        self.assertTrue(any("Ctrl-\\ detach" in m for m in mutated(edit)))

    def test_exit_76_without_reconnect_rejected(self):
        def edit(doc):
            doc["exit_codes"][-1]["meaning"] = "terminal connection lost"
        self.assertTrue(any("exit code 76" in m and "reconnect" in m for m in mutated(edit)))

    def test_token_mode_relaxed_rejected(self):
        def edit(doc):
            doc["token"]["file_mode"] = "0644"
        self.assertTrue(any("0600" in m for m in mutated(edit)))

    def test_token_in_argv_allowed_rejected(self):
        def edit(doc):
            doc["token"]["never_in_argv"] = False
        self.assertTrue(any("never_in_argv" in m for m in mutated(edit)))

    def test_token_env_override_removed_rejected(self):
        def edit(doc):
            doc["token"]["env_override"]["name"] = "SANDBOX_KEY"
        self.assertTrue(any("SANDBOX_TOKEN" in m for m in mutated(edit)))

    def test_missing_restore_path_rejected(self):
        def edit(doc):
            doc["terminal"]["raw_mode_restored_on_exit_paths"].remove("panic")
        self.assertTrue(any("closed path set" in m for m in mutated(edit)))

    def test_detach_nonzero_exit_rejected(self):
        def edit(doc):
            doc["terminal"]["detach_exit_code"] = 1
        self.assertTrue(any("detach must exit 0" in m for m in mutated(edit)))

    def test_network_drop_without_reconnect_rejected(self):
        def edit(doc):
            doc["terminal"]["network_drop"] = "exit 76 immediately on any network drop"
        self.assertTrue(any("auto-reconnect" in m or "reconnect" in m for m in mutated(edit)))

    def test_cold_recovery_as_old_session_rejected(self):
        def edit(doc):
            doc["terminal"]["cold_recovery"] = (
                "after cold resume the CLI shows the session as it was before")
        self.assertTrue(any("NEW session" in m for m in mutated(edit)))

    def test_telemetry_field_removed_rejected(self):
        def edit(doc):
            doc["telemetry"]["fields"] = ["terminal_ready_ms"]
        self.assertTrue(any("telemetry.fields" in m for m in mutated(edit)))

    def test_telemetry_promise_rejected(self):
        def edit(doc):
            doc["telemetry"]["target_not_promise"] = "terminal ready within 10 seconds, guaranteed"
        self.assertTrue(any("target" in m and "not a promise" in m for m in mutated(edit)))

    def test_release_without_checksum_rejected(self):
        def edit(doc):
            doc["install_artifacts"]["release_requires"] = [
                {"artifact": "version file", "required": True}]
        self.assertTrue(any("checksum" in m for m in mutated(edit)))

    def test_untested_platform_claimed_rejected(self):
        def edit(doc):
            doc["install_artifacts"]["platforms"][0]["tested"] = True
        self.assertTrue(any("before real testing" in m for m in mutated(edit)))

    def test_mocked_api_gap_rejected(self):
        def edit(doc):
            doc["api_gaps"] = "missing control-plane capabilities return a success stub for now"
        self.assertTrue(any("mocked success" in m for m in mutated(edit)))

    def test_oauth_login_rejected(self):
        def edit(doc):
            find_command(doc, "login")["no_oauth"] = "full OAuth device flow"
        self.assertTrue(any("no OAuth" in m for m in mutated(edit)))

    def test_unlinked_ticket_reference_rejected(self):
        def edit(doc):
            doc["api_gaps"] += " (see #99)"
        self.assertTrue(any("no integer entry" in m for m in mutated(edit)))

    def test_required_ticket_link_removed_rejected(self):
        def edit(doc):
            doc["related_tickets"] = [t for t in doc["related_tickets"]
                                      if t["ticket"] != 20]
        self.assertTrue(any("#20 must be linked" in m for m in mutated(edit)))


if __name__ == "__main__":
    unittest.main()
