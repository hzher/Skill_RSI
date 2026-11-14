"""Adapter planning cannot elevate metadata declarations into runtime capability."""
import json
import unittest

from tests import test_task_routing as routing_fixture
from training.adapter_design import design_adapter


class AdapterDesignTests(unittest.TestCase):
    def setUp(self):
        fixture = routing_fixture.TaskRoutingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        path = self.root / "training/toolathlon_seed_capabilities.json"
        value = json.loads(path.read_text())
        value["tasks"]["second-task"] = {"configuration_status": "inspected", "config_git_blob": "d" * 40,
            "config_sha256": "e" * 64, "needed_mcp_servers": ["woocommerce", "calendar"],
            "needed_local_tools": ["python_execute"], "preprocess_script_present": True,
            "evaluation_script_present": True, "file_asset_count": 0}
        path.write_text(json.dumps(value))

    def test_shared_service_names_do_not_enable_a_task_or_training(self):
        report = design_adapter(self.root, "second-task")
        self.assertEqual(report["service_names_represented_in_existing_profiles"], ["woocommerce"])
        self.assertEqual(report["declared_services_without_existing_mcp_profile"], ["calendar"])
        self.assertEqual(report["existing_task_profile_ids"], [])
        self.assertFalse(report["runtime_activation"])
        self.assertFalse(report["training_admission"])
        self.assertIn("unknown", report["method_compatibility"])

    def test_unknown_or_uninspected_configuration_is_not_a_plan(self):
        with self.assertRaises(ValueError): design_adapter(self.root, "missing")
        path = self.root / "training/toolathlon_seed_capabilities.json"
        value = json.loads(path.read_text())
        value["tasks"]["second-task"]["configuration_status"] = "unknown"
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError): design_adapter(self.root, "second-task")

    def test_source_or_profile_changes_cannot_silently_update_the_design(self):
        path = self.root / "configs/task_adapter_profiles.json"
        value = json.loads(path.read_text())
        value["automatic_training_admissions"] = 1
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError): design_adapter(self.root, "second-task")


if __name__ == "__main__": unittest.main()
