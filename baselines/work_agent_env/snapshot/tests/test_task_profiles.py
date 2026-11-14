from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from integrations.task_profiles import build_registry, resolve_profile, literal_assignment, MEAL_IMPLEMENTATION_FILES


class TaskProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fixtures = {"adapters/toolathlon_native.py": 'TASK_ID="unit-task"\nraise RuntimeError("not imported")\n',
                    "adapters/woocommerce_native.mjs": '// unit fixture, never executed\n',
                    "integrations/toolathlon_bridge.py": 'ALLOWED={"woocommerce":{"unit_read"}}\nraise RuntimeError("not imported")\n'}
        for name, text in fixtures.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.root / "configs").mkdir()
        (self.root / "configs/mainline_sources.lock.json").write_text(json.dumps({"sources": {"toolathlon_gym": {"commit": "a" * 40}}}))
        (self.root / "training").mkdir()
        row = {"configuration_status": "inspected", "config_git_blob": "b" * 40, "config_sha256": "c" * 64,
               "needed_mcp_servers": ["woocommerce", "excel"], "needed_local_tools": ["python_execute"]}
        (self.root / "training/toolathlon_seed_capabilities.json").write_text(json.dumps({"source": "toolathlon_gym", "commit": "a" * 40, "tasks": {"unit-task": row}}))
        self.add_meal_fixture()
        self.registry = build_registry(self.root)

    def add_meal_fixture(self):
        for name in MEAL_IMPLEMENTATION_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('TASK_ID="howtocook-meal-plan-gcal"\nMETHODS={"howtocook":("mcp_howtocook_getAllRecipes","mcp_howtocook_getRecipeById"),"google_calendar":("create_event","get_event","update_event","delete_event","list_events")}\n' if name.endswith("meal_plan_services.py") else '# source fixture, never imported\n')
        path = self.root / "training/toolathlon_seed_capabilities.json"
        metadata = json.loads(path.read_text())
        metadata["tasks"]["howtocook-meal-plan-gcal"] = {"configuration_status": "inspected", "config_git_blob": "d" * 40,
            "config_sha256": "e" * 64, "needed_mcp_servers": ["howtocook", "google_calendar", "excel", "filesystem"], "needed_local_tools": []}
        path.write_text(json.dumps(metadata))

    def test_meal_profile_is_isolated_only_and_retains_unexposed_services(self):
        result = resolve_profile(self.registry, root=self.root, task_id="howtocook-meal-plan-gcal", source_commit="a" * 40,
            task_config_sha256="e" * 64, resource_backend="isolated")
        self.assertEqual(result["profile"]["artifact_tools"], ["write_workbook", "read_workbook"])
        self.assertEqual(result["profile"]["declared_services_not_exposed_as_mcp"], ["excel", "filesystem"])
        with self.assertRaises(ValueError):
            resolve_profile(self.registry, root=self.root, task_id="howtocook-meal-plan-gcal", source_commit="a" * 40,
                task_config_sha256="e" * 64, resource_backend="shared")

    def resolve(self, registry=None, **changes):
        values = {"task_id": "unit-task", "source_commit": "a" * 40, "task_config_sha256": "c" * 64, "resource_backend": "shared"}
        return resolve_profile(registry or self.registry, root=self.root, **{**values, **changes})

    def test_shared_and_isolated_bind_same_profile_without_runtime_or_training_activation(self):
        shared, isolated = self.resolve(), self.resolve(resource_backend="isolated")
        self.assertEqual(shared["profile_sha256"], isolated["profile_sha256"])
        self.assertFalse(shared["runtime_activation"])
        self.assertFalse(shared["training_admission"])
        self.assertEqual(shared["profile"]["declared_services_not_exposed_as_mcp"], ["excel"])

    def test_task_source_config_or_backend_cannot_be_substituted(self):
        for changes in ({"task_id": "other-task"}, {"source_commit": "d" * 40}, {"task_config_sha256": "d" * 64},
                        {"resource_backend": "verified"}):
            with self.assertRaises(ValueError): self.resolve(**changes)

    def test_tool_expansion_or_source_edit_invalidates_profile(self):
        expanded = deepcopy(self.registry)
        expanded["profiles"]["unit-task-v1"]["mcp_methods"]["woocommerce"].append("unit_write")
        with self.assertRaises(ValueError): self.resolve(expanded)
        path = self.root / "adapters/toolathlon_native.py"
        path.write_text(path.read_text() + '# changed source\n')
        with self.assertRaises(ValueError): self.resolve()

    def test_dynamic_declarations_and_ambiguous_task_identity_are_never_executed(self):
        for raw in (b'TASK_ID = run_untrusted_code()', b'TASK_ID="one"\nTASK_ID="two"'):
            with self.assertRaises(ValueError): literal_assignment(raw, "TASK_ID")

    def test_declared_service_mismatch_prevents_profile_materialization(self):
        path = self.root / "integrations/toolathlon_bridge.py"
        path.write_text('ALLOWED={"unknown_service":{"unit_read"}}\n')
        with self.assertRaises(ValueError): build_registry(self.root)
