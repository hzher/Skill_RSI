"""Business bundle boundaries; fixture clients never claim actual MCP execution."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from integrations import meal_plan_services as services
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile
from workenv.core import ResourceError, save


class Tool:
    def __init__(self, name): self.name = name
    def model_dump(self, **kwargs): return {"name": self.name, "inputSchema": {"type": "object"}}


class Client:
    def __init__(self, names):
        self.names, self.calls, self.error = list(names), [], None
        self.concurrent, self.maximum = 0, 0

    async def list_tools(self): return SimpleNamespace(tools=[Tool(n) for n in self.names])

    async def call_tool(self, name, args):
        self.calls.append((name, deepcopy(args)))
        self.concurrent += 1
        self.maximum = max(self.maximum, self.concurrent)
        try:
            await asyncio.sleep(0)
            if self.error: raise self.error
            return SimpleNamespace(model_dump=lambda **kwargs: {"content": [{"type": "text", "text": "fixture response"}], "isError": False})
        finally: self.concurrent -= 1


class MealPlanServicesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.profile = EpisodeProfile(self.root, "b500f661-1cbb-4d00-8a34-31acb1101a65", 0)
        self.profile.base.mkdir(parents=True)
        save(self.profile.base / "cluster_owner.json", self.profile.record())
        self.cluster = EpisodeCluster(self.profile)
        self.cluster.started = True
        self.workspace = self.profile.base / "calendar-workspace"
        self.workspace.mkdir()
        save(self.profile.base / "calendar-initialized.json", {"state": "ready", "profile": self.profile.record(), "build_fingerprint": "calendar-fixture"})
        self.receipts = {"calendar": {"build_fingerprint": "calendar-fixture"}}
        self.bindings = {"fixture/component.json": "a" * 64}
        self.component_patch = patch.object(services, "components", return_value=(self.receipts, self.bindings, self.root))
        self.component_patch.start()
        self.addCleanup(self.component_patch.stop)
        self.recipe = Client([*services.METHODS["howtocook"], "random_tool_not_granted"])
        self.calendar = Client(services.METHODS["google_calendar"])
        self.closed = []

        def context(client, key):
            @asynccontextmanager
            async def enter(*args):
                record = {"closed": False, "fixture": True, "profile": self.profile.record(),
                    "before": {"pid": 1234, "status": "alive", "creation_id": "fixture-child"}}
                try: yield client, record
                finally:
                    record["closed"] = True
                    record["after"] = {"pid": 1234, "status": "not_found", "creation_id": None}
                    self.closed.append(key)
            return enter
        for name, client, key in [("recipe_session", self.recipe, "recipe"), ("calendar_session", self.calendar, "calendar")]:
            active = patch.object(services, name, context(client, key))
            active.start()
            self.addCleanup(active.stop)

    def bundle(self): return services.MealPlanServices(self.root, self.cluster)

    async def test_open_preserves_workbook_and_closes_both_original_clients(self):
        report = self.workspace / "Weekly_Meal_Plan.xlsx"
        report.write_bytes(b"retained fixture bytes")
        bundle = self.bundle()
        async with bundle.open():
            self.assertEqual(report.read_bytes(), b"retained fixture bytes")
            self.assertEqual(len(bundle.tools()), 7)
            self.assertNotIn("random_tool_not_granted", [t["name"] for t in bundle.tools()])
            self.assertTrue((self.profile.base / ".meal-services.lock").exists())
            dependent = json.loads((self.profile.base / "dependent_processes.json").read_text(encoding="utf-8"))
            self.assertEqual(dependent["state"], "open")
            self.assertEqual(set(dependent["children"]), set(services.METHODS))
        self.assertEqual(self.closed, ["calendar", "recipe"])
        self.assertEqual(bundle.state, "closed")
        self.assertFalse((self.profile.base / ".meal-services.lock").exists())
        with self.assertRaises(ResourceError):
            async with bundle.open(): self.fail("A service object was reopened")

    async def test_wrong_task_foreign_or_stopped_episode_is_rejected_before_launch(self):
        with self.assertRaises(ValueError): services.MealPlanServices(self.root, self.cluster, task_id="wc-sales-tax-summary")
        with self.assertRaises(ValueError): services.MealPlanServices(self.root / "foreign", self.cluster)
        self.cluster.started = False
        with self.assertRaises(ResourceError): self.bundle()
        self.assertEqual(self.closed, [])

    async def test_build_rebinding_and_failed_initialization_block_before_launch(self):
        bundle = self.bundle()
        with patch.object(services, "components", return_value=(self.receipts, {}, self.root)):
            with self.assertRaises(ResourceError):
                async with bundle.open(): self.fail("Changed build was accepted")
        self.assertEqual(self.closed, [])
        save(self.profile.base / "calendar-initialized.json", {"state": "initializing", "profile": self.profile.record()})
        with self.assertRaises(ResourceError): self.bundle()

    async def test_calls_are_serialized_and_unknown_methods_or_nonfinite_arguments_never_forward(self):
        bundle = self.bundle()
        async with bundle.open():
            for target in [("filesystem", "read_file", {}), ("howtocook", "random_tool_not_granted", {}), ("google_calendar", "create_event", {"x": float("nan")})]:
                with self.assertRaises(ValueError): await bundle.call(*target)
            self.assertEqual(self.calendar.calls, [])
            await asyncio.gather(*(bundle.call("google_calendar", "get_event", {"eventId": str(i)}) for i in range(6)))
            self.assertEqual(self.calendar.maximum, 1)
            self.assertEqual(bundle.calls, 6)

    async def test_schema_failure_unwinds_already_opened_clients(self):
        self.calendar.names.remove("create_event")
        bundle = self.bundle()
        with self.assertRaises(ResourceError):
            async with bundle.open(): self.fail("Incomplete original schemas were accepted")
        self.assertEqual(self.closed, ["calendar", "recipe"])
        self.assertEqual(bundle.state, "closed")

    async def test_partial_startup_failure_closes_first_client_and_keeps_unknown_state(self):
        @asynccontextmanager
        async def failed(*args):
            raise RuntimeError("Fixture calendar startup failure")
            yield
        bundle = self.bundle()
        with patch.object(services, "calendar_session", failed):
            with self.assertRaises(RuntimeError):
                async with bundle.open(): self.fail("Failed startup entered the bundle")
        self.assertEqual(self.closed, ["recipe"])
        self.assertEqual(bundle.state, "unknown")

    async def test_lost_mutation_reply_is_not_retried_and_blocks_further_calls_and_scoring(self):
        bundle = self.bundle()
        async with bundle.open():
            self.calendar.error = TimeoutError("Fixture lost reply")
            with self.assertRaises(ResourceError): await bundle.call("google_calendar", "create_event", {"summary": "fixture"})
            self.calendar.error = None
            with self.assertRaises(ResourceError): await bundle.call("google_calendar", "list_events", {})
            with self.assertRaises(ResourceError): await bundle.verify()
            with self.assertRaises(ResourceError): await bundle.write_workbook({})
            self.assertEqual(len(self.calendar.calls), 1)
        self.assertTrue(bundle.record()["outcome_unknown"])

    async def test_receipt_replacement_during_open_session_blocks_forwarding(self):
        bundle = self.bundle()
        async with bundle.open():
            with patch.object(services, "components", return_value=(self.receipts, {}, self.root)):
                with self.assertRaises(ResourceError): await bundle.call("google_calendar", "list_events", {})
            self.assertEqual(self.calendar.calls, [])

    async def test_call_budget_stops_before_another_original_call(self):
        bundle = self.bundle()
        async with bundle.open():
            bundle.calls = services.MAX_CALLS
            with self.assertRaises(ResourceError): await bundle.call("google_calendar", "list_events", {})
            self.assertEqual(self.calendar.calls, [])

    async def test_workbook_and_evaluation_delegate_to_the_same_owned_cluster(self):
        bundle = self.bundle()
        async with bundle.open():
            with patch.object(services, "write_report", return_value={"sha256": "fixture"}) as write, patch.object(services, "read_report", return_value={"bytes": 5}) as read, patch.object(services, "evaluate", return_value=({"task_passed": True}, self.workspace / "fixture.json")) as evaluate:
                payload = {"meal_plan": [], "shopping_list": []}
                await bundle.write_workbook(payload)
                await bundle.read_workbook()
                await bundle.verify()
                write.assert_called_once_with(self.root, self.cluster, payload)
                read.assert_called_once_with(self.root, self.cluster)
                evaluate.assert_called_once_with(self.root, self.cluster)

    async def test_competing_open_cannot_replace_the_active_child_ownership_record(self):
        first, second = self.bundle(), self.bundle()
        async with first.open():
            marker = self.profile.base / "dependent_processes.json"
            before = marker.read_bytes()
            with self.assertRaises(FileExistsError):
                async with second.open(): self.fail("Competing bundle entered a busy episode")
            self.assertEqual(marker.read_bytes(), before)
            await first.call("google_calendar", "list_events", {})

    def test_payload_budgets_reject_unserializable_and_oversized_inputs(self):
        for payload in ({"x": object()}, {"x": "a" * 100}):
            with self.assertRaises(ValueError): services.json_bytes(payload, 32)


class ComponentBindingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.receipts = {"recipe": {"source_commit": "a" * 40, "recipe_sha256": "b" * 64},
            "calendar": {"source_commit": "a" * 40}, "snapshot": {"source_commit": "a" * 40, "sha256": "b" * 64}}
        save(self.root / "configs/mainline_sources.lock.json", {"sources": {"toolathlon_gym": {"commit": "a" * 40}}})
        for key, name in services.COMPONENT_FILES.items(): save(self.root / name, self.receipts[key])
        for name in ("verify_component", "verify_build", "verify_snapshot"):
            active = patch.object(services, name, return_value=self.root)
            active.start()
            self.addCleanup(active.stop)

    def test_matching_components_retain_exact_receipt_byte_identities(self):
        receipts, hashes, base = services.components(self.root)
        self.assertEqual(receipts, self.receipts)
        self.assertEqual(set(hashes), set(services.COMPONENT_FILES.values()))
        self.assertEqual(base, self.root)

    def test_mismatched_source_or_recipe_snapshot_is_rejected(self):
        for changed in ({"source_commit": "c" * 40}, {"recipe_sha256": "d" * 64}):
            save(self.root / services.COMPONENT_FILES["recipe"], {**self.receipts["recipe"], **changed})
            with self.assertRaises(ValueError): services.components(self.root)

    def test_oversized_component_receipt_is_rejected_before_loading(self):
        (self.root / services.COMPONENT_FILES["recipe"]).write_bytes(b" " * (256 * 1024 + 1))
        with self.assertRaises(ValueError): services.components(self.root)
