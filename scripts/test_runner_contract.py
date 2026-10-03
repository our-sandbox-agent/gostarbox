"""Guard tests: each verifier rule must reject a contract that breaks it (CONTRIBUTING)."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from verify_runner_contract import check_contract  # noqa: E402

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "docs/contracts/runner-lifecycle.json").read_text())


def mutated(*editors):
    doc = copy.deepcopy(CONTRACT)
    for edit in editors:
        edit(doc)
    return check_contract(doc)


def find_transition(doc, source, target, trigger):
    return next(t for t in doc["transitions"]
                if (t["from"], t["to"], t["trigger"]) == (source, target, trigger))


class ShippedContract(unittest.TestCase):
    def test_shipped_contract_passes(self):
        self.assertEqual(check_contract(CONTRACT), [])


class RejectsBrokenContracts(unittest.TestCase):
    def test_undefined_state_rejected(self):
        def edit(doc):
            del doc["states"]["Suspend"]
        self.assertTrue(any("is not defined" in m for m in mutated(edit)))

    def test_duplicate_transition_rejected(self):
        def edit(doc):
            doc["transitions"].append(copy.deepcopy(doc["transitions"][0]))
        self.assertTrue(any("duplicate transition" in m for m in mutated(edit)))

    def test_missing_requires_rejected(self):
        def edit(doc):
            del find_transition(doc, "Active", "Suspending", "suspend")["requires"]
        self.assertTrue(any("requires must be a non-empty list" in m for m in mutated(edit)))

    def test_unknown_trigger_rejected(self):
        def edit(doc):
            find_transition(doc, "Active", "Idle", "set_idle")["trigger"] = "vibecode"
        self.assertTrue(any("unknown trigger" in m for m in mutated(edit)))

    def test_unknown_error_code_rejected(self):
        def edit(doc):
            find_transition(doc, "Active", "Error",
                            "memory_termination_confirmed")["error_codes"] = ["oom_boom"]
        self.assertTrue(any("not defined by the ADRs" in m for m in mutated(edit)))

    def test_terminal_state_exit_rejected(self):
        def edit(doc):
            doc["transitions"].append({"from": "Destroyed", "to": "Active", "trigger": "create",
                                       "requires": ["recreate"], "side_effects": ["new instance"]})
        self.assertTrue(any("terminal state Destroyed" in m for m in mutated(edit)))

    def test_unreachable_state_rejected(self):
        def edit(doc):
            doc["transitions"] = [t for t in doc["transitions"]
                                  if (t["from"], t["to"]) != ("Suspending", "Suspend")]
        self.assertTrue(any("unreachable" in m for m in mutated(edit)))

    def test_lost_direct_to_destroyed_rejected(self):
        def edit(doc):
            doc["transitions"].append({"from": "Lost", "to": "Destroyed", "trigger": "destroy",
                                       "requires": ["fencing done"],
                                       "side_effects": ["remove resources"]})
        self.assertTrue(any("not go directly to Destroyed" in m for m in mutated(edit)))

    def test_lost_exit_without_fencing_rejected(self):
        def edit(doc):
            find_transition(doc, "Lost", "Destroying", "destroy")["requires"] = ["operator click"]
        self.assertTrue(any("fencing/reconciliation" in m for m in mutated(edit)))

    def test_lost_auto_release_rejected(self):
        def edit(doc):
            find_transition(doc, "Lost", "Destroying", "destroy")["side_effects"].append(
                "release reserved capacity")
        self.assertTrue(any("must not release capacity" in m for m in mutated(edit)))

    def test_error_entry_without_evidence_rejected(self):
        def edit(doc):
            find_transition(doc, "Suspending", "Error", "suspend")["side_effects"] = [
                "nothing recorded"]
        self.assertTrue(any("last_confirmed_state" in m for m in mutated(edit)))

    def test_error_silent_recovery_rejected(self):
        def edit(doc):
            doc["transitions"].append({"from": "Error", "to": "Idle", "trigger": "set_idle",
                                       "requires": ["idle policy"],
                                       "side_effects": ["emit policy.applied"]})
        self.assertTrue(any("Error must not go directly" in m for m in mutated(edit)))

    def test_memory_code_on_wrong_transition_rejected(self):
        def edit(doc):
            find_transition(doc, "Active", "Idle", "set_idle")["error_codes"] = [
                "memory_limit_terminated"]
        self.assertTrue(any("memory_limit_terminated only applies" in m for m in mutated(edit)))

    def test_recovery_retry_code_missing_rejected(self):
        def edit(doc):
            find_transition(doc, "Resuming", "Error", "resume")["error_codes"] = []
        self.assertTrue(any("recovery_retry_exhausted" in m for m in mutated(edit)))

    def test_refusal_creating_operation_rejected(self):
        def edit(doc):
            next(r for r in doc["refusals"]
                 if r["id"] == "credentials-required")["creates_operation"] = True
        self.assertTrue(any("must not create an operation" in m for m in mutated(edit)))

    def test_unknown_refusal_code_rejected(self):
        def edit(doc):
            next(r for r in doc["refusals"]
                 if r["id"] == "version-or-operation-conflict")["code"] = "version_skew"
        self.assertTrue(any("not defined by the ADRs" in m for m in mutated(edit)))

    def test_capacity_inputs_shape_rejected(self):
        def edit(doc):
            del doc["capacity_admission"]["inputs"]["volume_bytes"]
        self.assertTrue(any("milli_cpu, memory_bytes, volume_bytes" in m for m in mutated(edit)))

    def test_capacity_exceeded_creates_operation_rejected(self):
        def edit(doc):
            doc["capacity_admission"]["on_exceeded"]["creates_operation"] = True
        self.assertTrue(any("capacity_exceeded" in m for m in mutated(edit)))

    def test_required_invariant_removed_rejected(self):
        def edit(doc):
            doc["invariants"] = [i for i in doc["invariants"]
                                 if i["id"] != "generation_fencing"]
        self.assertTrue(any("generation_fencing" in m for m in mutated(edit)))


if __name__ == "__main__":
    unittest.main()
