from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
import hashlib
from unittest.mock import Mock
from workenv.core import ResourceError, save

from integrations import isolated_gateway as gateway


class IsolatedGatewayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="isolated-gateway-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        item = patch.object(gateway, "ROOT", self.root)
        item.start()
        self.addCleanup(item.stop)
        self.server = gateway.IsolatedGateway()
        self.body = {"resources_session_id": str(uuid.uuid4()), "episode_id": {"rollout_id": "unit"},
            "task_id": {"taskset": "toolathlon_gym:unit", "task_id": "wc-sales-tax-summary"}, "task_data": {}}

    def test_foreign_sources_and_extra_payload_never_allocate_a_worker(self):
        for change in ({"task_data": {"answer": "forbidden"}}, {"task_id": {"taskset": "verified:unit", "task_id": "wc-sales-tax-summary"}}):
            with self.assertRaises(ValueError): self.server.dispatch("/seed_session", {**self.body, **change})
        self.assertEqual(self.server.ledger.rows, {})
        self.assertFalse((self.root / ".runtime/native-episodes").exists())

    def test_close_before_seed_is_durable_and_cannot_be_rebound(self):
        bound = {k: self.body[k] for k in ("resources_session_id", "episode_id")}
        self.server.dispatch("/close_session", bound)
        reloaded = gateway.IsolatedGateway()
        with self.assertRaises(gateway.IsolatedConflict): reloaded.dispatch("/seed_session", self.body)
        with self.assertRaises(gateway.IsolatedConflict):
            reloaded.dispatch("/close_session", {**bound, "episode_id": {"rollout_id": "other"}})

    def test_two_busy_slots_reject_new_seed_before_reading_or_creating_database_resources(self):
        self.server.sessions = {"unit-one": {"slot": 0}, "unit-two": {"slot": 1}}
        with self.assertRaises(gateway.IsolatedConflict): self.server.dispatch("/seed_session", self.body)
        self.assertEqual(self.server.ledger.rows, {})

    def test_status_remains_available_while_resource_startup_holds_the_mutation_lock(self):
        entered, release = threading.Event(), threading.Event()
        def startup():
            with self.server.lock:
                entered.set()
                release.wait(timeout=3)
        thread = threading.Thread(target=startup)
        thread.start()
        self.assertTrue(entered.wait(timeout=1))
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.server.status)
                try: self.assertEqual(future.result(timeout=1)["resource_slots"], 2)
                finally: release.set()
        finally:
            release.set()
            thread.join(timeout=3)

    def test_meal_seed_metadata_cannot_substitute_task_configuration_or_data_scope(self):
        task = "howtocook-meal-plan-gcal"
        save(self.root / "configs/mainline_sources.lock.json", {"sources": {"toolathlon_gym": {"commit": "a" * 40}}})
        save(self.root / "training/toolathlon_seed_capabilities.json", {"tasks": {task: {"config_sha256": "b" * 64}}})
        seed = self.root / ".runtime/native-episodes/seed-cache/unit.sql"
        seed.parent.mkdir(parents=True)
        seed.write_bytes(b"-- explicitly empty schema unit fixture\n")
        record = {"task_commit": "a" * 40, "task_id": task, "schemas": ["gcal"], "schema_only": True,
            "task_config_sha256": "b" * 64, "seed_ref": seed.relative_to(self.root).as_posix(), "sha256": hashlib.sha256(seed.read_bytes()).hexdigest()}
        path = self.root / ".runtime/nemo-vm/meal-plan-seed.json"
        save(path, record)
        self.assertEqual(self.server._seed_sql(task), seed)
        for change in ({"task_commit": "c" * 40}, {"task_id": "wc-sales-tax-summary"}, {"schemas": ["wc"]},
                       {"schema_only": False}, {"task_config_sha256": "c" * 64}, {"sha256": "c" * 64}):
            save(path, {**record, **change})
            with self.subTest(change=change), self.assertRaises(ResourceError): self.server._seed_sql(task)

    def test_meal_task_selects_its_source_bound_worker_before_activation(self):
        self.server.task_router = Mock()
        self.server.task_router.authorize.return_value = {"task_id": "howtocook-meal-plan-gcal"}
        body = {**self.body, "task_id": {**self.body["task_id"], "task_id": "howtocook-meal-plan-gcal"}}
        with patch.object(self.server, "_seed_sql", return_value=self.root / "unit.sql") as seed, patch.object(gateway, "EpisodeWorkerClient") as worker:
            worker.return_value.call.return_value = {"status": 200, "result": {"resources_session_id": body["resources_session_id"]}}
            self.server.dispatch("/seed_session", body)
            seed.assert_called_once_with("howtocook-meal-plan-gcal")
            self.assertEqual(worker.call_args.kwargs["task_id"], "howtocook-meal-plan-gcal")
        self.assertEqual(self.server.ledger.rows[body["resources_session_id"]]["state"], "active")
