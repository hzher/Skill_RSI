"""Public packages, genuine evaluator subprocesses and NeMo measurement adaptation."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from integrations.benchmark_tasks import JsonTaskPlugin, make_task, make_task_set
from integrations.benchmark_tasks.package_source import PackageTaskPlugin
from integrations.verifiers import CommandJSONVerifier, VerifierRegistry, VerifierService, normalize_score
from integrations.verifiers.config import configured_service
from integrations.verifiers.nemo import evaluate_for_nemo, deployment
from scripts.evaluate_tasks import evaluate

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "integrations/benchmarks/file_report"


class TaskPackageTests(unittest.TestCase):
    def test_template_imports_without_executing_evaluator_or_reading_answers(self):
        task = PackageTaskPlugin(EXAMPLE).load()[0]
        self.assertEqual(task["uid"], "file_report:greeting")
        self.assertEqual(task["tools"], [{"kind": "local_tool", "name": "write_file"}])
        self.assertNotIn("json.load", json.dumps(task))
        self.assertFalse(task["metadata"]["training_admitted"])

    def test_canonical_taskset_roundtrip_preserves_provenance(self):
        tasks = make_task_set(PackageTaskPlugin(EXAMPLE).load())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.json"
            path.write_text(json.dumps(tasks))
            self.assertEqual(JsonTaskPlugin(path).load(), tasks["tasks"])
            tasks["tasks"][0]["prompt"] = "tampered"
            path.write_text(json.dumps(tasks))
            with self.assertRaises(ValueError): JsonTaskPlugin(path).load()

    def test_package_rejects_escaping_symlink_and_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = json.loads((EXAMPLE / "manifest.json").read_text())
            manifest["tasks"] *= 2
            (root / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError): PackageTaskPlugin(root).load()
            manifest["tasks"] = ["escape"]
            (root / "manifest.json").write_text(json.dumps(manifest))
            (root / "tasks").mkdir()
            (root / "tasks/escape").symlink_to(EXAMPLE / "tasks/greeting", target_is_directory=True)
            with self.assertRaises(ValueError): PackageTaskPlugin(root).load()


class VerifierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.task = PackageTaskPlugin(EXAMPLE).load()[0]
        self.tasks = make_task_set([self.task])
        self.script = EXAMPLE / "tasks/greeting/evaluation/main.py"
        self.settings = {"argv": [sys.executable, str(self.script)], "cwd": str(EXAMPLE),
            "files": {str(self.script.relative_to(EXAMPLE)): hashlib.sha256(self.script.read_bytes()).hexdigest()},
            "timeout": 5}
        self.config = self.workspace / "verifiers.json"
        self.config.write_text(json.dumps({"schema_version": "workagent-verifiers-v1",
            "backends": {"command_json": self.settings}}))
        self.service = configured_service(self.config)
        self.context = {"workspace": str(self.workspace)}

    async def test_original_adapter_subprocess_distinguishes_failure_and_success(self):
        failed = await self.service.evaluate(self.task, self.context)
        self.assertTrue(failed["valid"])
        self.assertEqual(failed["reward"], 0)
        (self.workspace / "result.txt").write_bytes(b"hello\n")
        passed = await self.service.evaluate(self.task, self.context)
        self.assertEqual(passed["reward"], 1)
        self.assertTrue(passed["passed"])
        self.assertEqual(passed["raw"], {"pass": True})
        self.assertFalse(passed["training_admitted"])

    async def test_changed_evaluator_source_is_not_scored(self):
        copied = self.workspace / "eval.py"
        copied.write_text("print('{\"pass\": true}')\n")
        handler = CommandJSONVerifier(argv=[sys.executable, str(copied)], cwd=self.workspace,
            files={"eval.py": hashlib.sha256(copied.read_bytes()).hexdigest()})
        registry = VerifierRegistry()
        registry.register("command_json", handler, version="owned-test")
        copied.write_text("print('{\"pass\": false}')\n")
        score = await VerifierService(registry).evaluate(self.task, self.context)
        self.assertFalse(score["valid"])
        self.assertIsNone(score["reward"])

    async def test_exception_malformed_output_timeout_are_unknown(self):
        for code in ("raise RuntimeError()", "print('not-json')", "import time; time.sleep(5)"):
            script = self.workspace / "eval.py"
            script.write_text(code)
            handler = CommandJSONVerifier(argv=[sys.executable, str(script)], cwd=self.workspace,
                files={"eval.py": hashlib.sha256(script.read_bytes()).hexdigest()}, timeout=.1)
            registry = VerifierRegistry()
            registry.register("command_json", handler, version="fixture")
            score = await VerifierService(registry, timeout=2).evaluate(self.task, self.context)
            self.assertIsNone(score["reward"])
            self.assertTrue(score["mask_sample"])

    async def test_taskset_preflights_all_bindings_before_any_evaluation(self):
        calls = []
        registry = VerifierRegistry()
        registry.register("command_json", lambda task, context: calls.append(task), version="fixture")
        other = deepcopy(self.task)
        other.update(task_id="other", uid="file_report:other")
        other["evaluation"]["backend"] = "missing"
        with self.assertRaises(ValueError):
            await evaluate(make_task_set([self.task, other]), VerifierService(registry),
                {self.task["uid"]: {}, other["uid"]: {}})
        self.assertEqual(calls, [])

    async def test_nemo_dispatch_returns_measurement_with_original_score(self):
        path = self.workspace / "tasks.json"
        path.write_text(json.dumps(self.tasks))
        (self.workspace / "result.txt").write_bytes(b"hello\n")
        result = await evaluate_for_nemo(task_set_path=str(path), verifier_config_path=str(self.config),
            task_id=self.task["uid"], context=self.context)
        self.assertEqual(result["reward"], 1)
        self.assertFalse(result["mask_sample"])
        self.assertEqual(result["evaluation"]["task_uid"], self.task["uid"])
        with self.assertRaises(ValueError):
            await evaluate_for_nemo(task_set_path=str(path), verifier_config_path=str(self.config),
                task_id="wrong", context=self.context)
        deployment.cache_clear()

    def test_normalization_preserves_continuous_reward_and_masks_infrastructure(self):
        self.assertEqual(normalize_score({"reward": .375})["reward"], .375)
        for raw in ({"pass": None}, {"pass": False, "failure": "evaluation_error"},
                    {"reward": 0, "mask_sample": True}, {"pass": False, "failure": "log_file_not_found"}):
            self.assertIsNone(normalize_score(raw)["reward"])
        for raw in ({"reward": float("nan")}, {"pass": "true"}, {"reward": True}):
            with self.assertRaises(ValueError): normalize_score(raw)
