"""Guard tests: each verifier rule must reject a contract that breaks it (CONTRIBUTING)."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from verify_persistence_contract import check_contract  # noqa: E402

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "docs/contracts/workspace-persistence.json").read_text())


def mutated(*editors):
    doc = copy.deepcopy(CONTRACT)
    for edit in editors:
        edit(doc)
    return check_contract(doc)


def find_event(doc, event_id):
    return next(e for e in doc["events"] if e["id"] == event_id)


class ShippedContract(unittest.TestCase):
    def test_shipped_contract_passes(self):
        self.assertEqual(check_contract(CONTRACT), [])


class RejectsBrokenContracts(unittest.TestCase):
    def test_suspend_dropping_volume_rejected(self):
        def edit(doc):
            event = find_event(doc, "cold_suspend")
            event["preserves"] = [r for r in event["preserves"] if r != "home_volume"]
        self.assertTrue(any("must preserve home_volume" in m for m in mutated(edit)))

    def test_destroy_deleting_snapshot_rejected(self):
        def edit(doc):
            event = find_event(doc, "destroy")
            event["preserves"] = []
            event["drops"].append("snapshot")
        self.assertTrue(any("must preserve snapshot" in m for m in mutated(edit)))

    def test_resource_both_preserved_and_dropped_rejected(self):
        def edit(doc):
            event = find_event(doc, "cold_suspend")
            event["drops"].append("workspace_volume")
        self.assertTrue(any("both preserved and dropped" in m for m in mutated(edit)))

    def test_undecided_resource_rejected(self):
        def edit(doc):
            event = find_event(doc, "memory_termination_recreation")
            event["drops"] = []
        self.assertTrue(any("no preserve/drop decision" in m for m in mutated(edit)))

    def test_resume_preserving_runtime_rejected(self):
        def edit(doc):
            event = find_event(doc, "resume")
            event["drops"] = []
            event["preserves"].append("sandbox_runtime")
        self.assertTrue(any("must drop sandbox_runtime" in m for m in mutated(edit)))

    def test_snapshot_expiry_touching_volumes_rejected(self):
        def edit(doc):
            event = find_event(doc, "snapshot_ttl_expiry")
            event["preserves"] = [r for r in event["preserves"] if r != "workspace_volume"]
            event["drops"].append("workspace_volume")
        self.assertTrue(any("snapshot_ttl_expiry': must preserve workspace_volume" in m
                            for m in mutated(edit)))

    def test_resize_dropping_resource_rejected(self):
        def edit(doc):
            event = find_event(doc, "volume_resize")
            event["preserves"] = [r for r in event["preserves"] if r != "snapshot"]
            event["drops"].append("snapshot")
        self.assertTrue(any("volume_resize': must preserve snapshot" in m for m in mutated(edit)))

    def test_unknown_event_rejected(self):
        def edit(doc):
            clone = copy.deepcopy(find_event(doc, "resume"))
            clone["id"] = "warm_suspend"
            doc["events"].append(clone)
        self.assertTrue(any("unknown event id" in m for m in mutated(edit)))

    def test_missing_event_rejected(self):
        def edit(doc):
            doc["events"] = [e for e in doc["events"] if e["id"] != "volume_resize"]
        self.assertTrue(any("is missing from the contract" in m for m in mutated(edit)))

    def test_recreation_losing_session_record_rejected(self):
        def edit(doc):
            find_event(doc, "memory_termination_recreation")["details"]["home_volume"] = \
                "the approved home volume is kept"
        self.assertTrue(any("session ID/cwd" in m for m in mutated(edit)))

    def test_required_invariant_removed_rejected(self):
        def edit(doc):
            doc["invariants"] = [i for i in doc["invariants"]
                                 if i["id"] != "destroy_does_not_imply_snapshot_deletion"]
        self.assertTrue(any("destroy_does_not_imply_snapshot_deletion" in m for m in mutated(edit)))

    def test_cold_restart_no_reclone_without_work_image_reference_rejected(self):
        def edit(doc):
            doc["cold_restart"]["no_reclone_no_overwrite"] = \
                "skip clone when the workspace is non-empty"
        self.assertTrue(any("work-image contract" in m for m in mutated(edit)))

    def test_cold_restart_session_via_docker_start_rejected(self):
        def edit(doc):
            doc["cold_restart"]["session_id_stable"] = \
                "the session ID survives container restart"
        self.assertTrue(any("docker start" in m for m in mutated(edit)))


if __name__ == "__main__":
    unittest.main()
