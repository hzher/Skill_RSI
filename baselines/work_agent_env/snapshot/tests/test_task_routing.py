"""Source-bound routing rejects drift before starting a database or worker."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from integrations.task_profiles import build_registry, MEAL_IMPLEMENTATION_FILES
from integrations.task_routing import TaskRouter
from integrations import isolated_gateway
from workenv.core import ResourceError


class TaskRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        files = {"adapters/toolathlon_native.py": 'TASK_ID="wc-sales-tax-summary"\n',
                 "adapters/woocommerce_native.mjs": '// fixture\n',
                 "integrations/toolathlon_bridge.py": 'ALLOWED={"woocommerce":{"woo_orders_list"}}\n',
                 "configs/mainline_sources.lock.json": json.dumps({"sources": {"toolathlon_gym": {"commit": "a" * 40}}}),
                 "training/toolathlon_seed_capabilities.json": json.dumps({"source": "toolathlon_gym", "commit": "a" * 40,
                     "tasks": {"wc-sales-tax-summary": {"configuration_status": "inspected", "config_git_blob": "b" * 40,
                         "config_sha256": "c" * 64, "needed_mcp_servers": ["woocommerce"], "needed_local_tools": []}}})}
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        for name in MEAL_IMPLEMENTATION_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('TASK_ID="howtocook-meal-plan-gcal"\nMETHODS={"howtocook":("mcp_howtocook_getAllRecipes","mcp_howtocook_getRecipeById"),"google_calendar":("create_event","get_event","update_event","delete_event","list_events")}\n' if name.endswith("meal_plan_services.py") else '# source fixture, never imported\n')
        path = self.root / "training/toolathlon_seed_capabilities.json"
        metadata = json.loads(path.read_text())
        metadata["tasks"]["howtocook-meal-plan-gcal"] = {"configuration_status": "inspected", "config_git_blob": "d" * 40,
            "config_sha256": "e" * 64, "needed_mcp_servers": ["howtocook", "google_calendar"], "needed_local_tools": []}
        path.write_text(json.dumps(metadata))
        (self.root / "configs/task_adapter_profiles.json").write_text(json.dumps(build_registry(self.root)))
        self.seed = {"resources_session_id": str(uuid.uuid4()), "episode_id": {"rollout_id": "unit"},
                     "task_id": {"taskset": "toolathlon_gym:unit", "task_id": "wc-sales-tax-summary"}, "task_data": {}}

    def test_both_backends_bind_same_source_and_return_independent_metadata(self):
        shared, isolated = [TaskRouter(self.root, backend).authorize(self.seed) for backend in ("shared", "isolated")]
        self.assertEqual(shared["profile_sha256"], isolated["profile_sha256"])
        self.assertEqual(shared["resource_backend"], "shared")
        self.assertEqual(isolated["resource_backend"], "isolated")
        self.assertFalse(shared["training_admission"])
        router = TaskRouter(self.root, "shared")
        first = router.authorize(self.seed)
        first["mcp_methods"]["woocommerce"].append("forbidden")
        self.assertNotIn("forbidden", router.authorize(self.seed)["mcp_methods"]["woocommerce"])

    def test_foreign_namespace_unknown_task_and_payload_are_rejected(self):
        router = TaskRouter(self.root, "shared")
        changes = ({"task_id": {**self.seed["task_id"], "taskset": "toolathlon_verified:unit"}},
                   {"task_id": {**self.seed["task_id"], "task_id": "other-task"}},
                   {"task_data": []}, {"task_data": {"answer": "no"}}, {"extra": "no"},
                   {"task_id": {**self.seed["task_id"], "source_commit": "a" * 40}})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                router.authorize({**self.seed, **change})

    def test_stale_implementation_prevents_startup_or_new_allocation(self):
        router = TaskRouter(self.root, "shared")
        path = self.root / "adapters/woocommerce_native.mjs"
        path.write_text('// replaced fixture\n')
        with self.assertRaises(ResourceError): router.authorize(self.seed)
        with self.assertRaises(ResourceError): TaskRouter(self.root, "shared")

    def test_regenerated_profile_requires_idle_reload(self):
        router = TaskRouter(self.root, "isolated")
        (self.root / "adapters/woocommerce_native.mjs").write_text('// next version\n')
        (self.root / "configs/task_adapter_profiles.json").write_text(json.dumps(build_registry(self.root)))
        with self.assertRaises(ResourceError): router.authorize(self.seed)
        self.assertEqual(TaskRouter(self.root, "isolated").authorize(self.seed)["resource_backend"], "isolated")

    def test_gateway_router_failure_precedes_seed_read_and_worker_spawn(self):
        with patch.object(isolated_gateway, "ROOT", self.root):
            router = Mock()
            router.authorize.side_effect = ResourceError("stale fixture")
            gateway = isolated_gateway.IsolatedGateway(task_router=router)
            with patch.object(gateway, "_seed_sql") as seed_sql, patch.object(isolated_gateway, "EpisodeWorkerClient") as worker:
                with self.assertRaises(ResourceError): gateway.dispatch("/seed_session", deepcopy(self.seed))
                seed_sql.assert_not_called()
                worker.assert_not_called()
            self.assertEqual(gateway.ledger.rows, {})
            self.assertFalse((self.root / ".runtime/native-episodes").exists())


if __name__ == "__main__": unittest.main()
