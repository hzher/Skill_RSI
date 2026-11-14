"""The meal adapter reuses resource binding, ledger and mutation no-replay rules."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from integrations.resource_ledger import ResourceLedger
from integrations.episode_worker_client import EpisodeWorkerClient
from workenv.core import ResourceError, save
try:
    from integrations import toolathlon_bridge as bridge
except ImportError:
    bridge = None


@unittest.skipIf(bridge is None, "Original task optional dependencies are not installed")
class MealWorkerContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sid = str(uuid4())
        self.run = self.root / ".runtime/native-episodes" / self.sid / "meal-task-session"
        self.run.mkdir(parents=True)
        save(self.run / "run.json", {"task_id": "howtocook-meal-plan-gcal"})
        self.seed = {"resources_session_id": self.sid, "episode_id": {"rollout_id": "unit", "attempt": 0},
            "task_id": {"taskset": "toolathlon_gym:unit", "task_id": "howtocook-meal-plan-gcal"}, "task_data": {}}
        self.bound = {k: self.seed[k] for k in ("resources_session_id", "episode_id")}
        self.adapter = SimpleNamespace(task_id="howtocook-meal-plan-gcal", new_run=Mock(return_value=self.run),
            seed=AsyncMock(return_value={"calendar_events": 0}), close=AsyncMock(),
            tools=Mock(return_value={"tools": [], "artifact_tools": []}), call=AsyncMock(return_value={"isError": False}),
            write=AsyncMock(return_value={"sha256": "fixture"}), read=AsyncMock(return_value={"base64": "fixture"}),
            verify=AsyncMock(return_value=({"evaluation_status": "completed", "task_passed": False}, self.run / "evaluation.json")))
        owner = patch.object(bridge.native, "ROOT", self.root)
        owner.start()
        self.addCleanup(owner.stop)
        router = patch.object(bridge, "TaskRouter")
        router.start().return_value.authorize.return_value = {"profile_id": "unit-meal-profile"}
        self.addCleanup(router.stop)
        self.resources = bridge.Resources(ResourceLedger(self.run.parent / "resources-session.json", self.root),
            resource_backend="isolated", adapter=self.adapter)

    async def start(self): return await self.resources.dispatch("/seed_session", self.seed)

    async def test_one_seed_reuses_the_existing_ledger_and_cannot_rebind(self):
        await self.start()
        await self.start()
        self.adapter.seed.assert_awaited_once_with(self.run)
        self.assertEqual(self.resources.ledger.rows[self.sid]["state"], "active")
        changed = {**self.seed, "episode_id": {"rollout_id": "different"}}
        with self.assertRaises(bridge.Conflict): await self.resources.dispatch("/seed_session", changed)
        with self.assertRaises(ValueError):
            await self.resources.dispatch("/seed_session", {**self.seed, "task_id": {**self.seed["task_id"], "task_id": "wc-sales-tax-summary"}})

    async def test_completed_mutations_are_cached_and_rebound_requests_are_rejected(self):
        await self.start()
        body = {**self.bound, "request_id": str(uuid4()), "workbook": {"meal_plan": [], "shopping_list": []}}
        first = await self.resources.dispatch("/write_workbook", body)
        self.assertEqual(await self.resources.dispatch("/write_workbook", body), first)
        self.adapter.write.assert_awaited_once()
        with self.assertRaises(bridge.Conflict):
            await self.resources.dispatch("/write_workbook", {**body, "workbook": {"other": []}})

    async def test_uncertain_mutation_is_not_replayed(self):
        await self.start()
        self.adapter.write.side_effect = ResourceError("fixture uncertain commit")
        body = {**self.bound, "request_id": str(uuid4()), "workbook": {}}
        with self.assertRaises(ResourceError): await self.resources.dispatch("/write_workbook", body)
        with self.assertRaises(bridge.Conflict): await self.resources.dispatch("/write_workbook", body)
        self.adapter.write.assert_awaited_once()

    async def test_tools_and_read_use_the_bound_adapter_and_report_upload_stays_rejected(self):
        await self.start()
        result = await self.resources.dispatch("/tools", self.bound)
        self.assertEqual(result["task_profile"]["profile_id"], "unit-meal-profile")
        self.assertEqual(await self.resources.dispatch("/read_workbook", self.bound), {"result": {"base64": "fixture"}})
        with self.assertRaises(ValueError): await self.resources.dispatch("/write_report", {**self.bound, "request_id": str(uuid4()), "base64": "Zg=="})
        with self.assertRaises(bridge.Conflict): await self.resources.dispatch("/read_workbook", {**self.bound, "episode_id": {"rollout_id": "other"}})

    async def test_semantic_failure_is_zero_and_unknown_is_masked_without_default_reward(self):
        await self.start()
        failed = await self.resources.dispatch("/verify", self.bound)
        self.assertEqual((failed["reward"], failed["mask_sample"]), (0.0, False))
        self.adapter.verify.return_value = ({"evaluation_status": "unknown", "task_passed": None}, self.run / "evaluation.json")
        unknown = await self.resources.dispatch("/verify", self.bound)
        self.assertEqual((unknown["reward"], unknown["mask_sample"]), (None, True))

    async def test_failed_close_remains_closing_then_preserves_tombstone(self):
        await self.start()
        self.adapter.close.side_effect = ResourceError("fixture child closure unknown")
        with self.assertRaises(ResourceError): await self.resources.dispatch("/close_session", self.bound)
        self.assertEqual(self.resources.ledger.rows[self.sid]["state"], "closing")
        self.assertEqual(self.resources.active, self.sid)
        self.adapter.close.side_effect = None
        await self.resources.dispatch("/close_session", self.bound)
        await self.resources.dispatch("/close_session", self.bound)
        self.assertEqual(self.resources.ledger.rows[self.sid]["state"], "closed")
        self.assertEqual(self.adapter.close.await_count, 2)
        with self.assertRaises(bridge.Conflict): await self.start()

    def test_shared_adapter_rejected_and_unknown_task_does_not_spawn(self):
        with self.assertRaises(ValueError): bridge.Resources(resource_backend="shared", adapter=self.adapter)
        with patch("integrations.episode_worker_client.subprocess.Popen") as spawn:
            with self.assertRaises(ValueError): EpisodeWorkerClient(self.root, slot=0, seed_sql=self.root / "seed.sql", task_id="unknown-task")
            spawn.assert_not_called()
