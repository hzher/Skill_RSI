"""Boundary tests for real trace/score transport, candidate isolation and attribution."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations.benchmark_tasks import digest, load_task_set
from integrations.benchmark_tasks.store import TaskStore
from integrations.executors import ExecutorOptions
from integrations.executors.api import APIExecutor
from integrations.experience import ExperienceStore, curator_view
from integrations.skill_generator.editor import SkillEditor
from integrations.skill_generator.attributor import attribute
from integrations.skill_library import SkillLibrary, SkillConflict
from scripts.skills import ingest_run
from tests.test_generic_runtime import install, Driver

CONTENT = '---\nname: Verify output\ndescription: Check deliverables before finishing.\n---\n# Workflow\nRead the output and compare it to the task requirements.\n'


class ExperienceSkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        install(self.root)
        self.tasks = load_task_set(TaskStore(self.root).register("file_report")["task_set"])
        self.task = self.tasks["tasks"][0]
        self.store = ExperienceStore(self.root / ".runtime/experience")
        self.library = SkillLibrary(self.root / ".runtime/skill_library")
        self.editor = SkillEditor(self.store, self.library)
        self.episode = self.root / "episode"
        (self.episode / "workspace").mkdir(parents=True)
        (self.episode / "workspace/result.txt").write_text("output")

    def capture(self, evaluation=None):
        return self.store.capture(task=self.task, receipt={"task_uid": self.task["uid"],
            "task_sha256": digest(self.task), "evaluation": evaluation, "model_replies": 1},
            trajectory=[{"role": "assistant", "text": "", "calls": [{"id": "a", "name": "write", "arguments": {"path": "result.txt"}}], "usage": {"total_tokens": 12}},
                        {"role": "tool", "id": "a", "content": "preview"}],
            calls=[{"name": "write", "arguments": {"path": "result.txt"}, "public_call_id": "a", "output": "full observation"}],
            directory=self.episode, executor={"model": "test-model"})["id"]

    def candidate(self, identity=None, parent=None):
        request = self.editor.prepare(identity or self.capture(), generator_policy_id="generator-v1", parent=parent)
        response = json.dumps([{"name": "new_skill_insert", "arguments": {"skill_name": "Verify output", "content": CONTENT}}])
        return self.editor.apply(request, response)

    def test_unknown_is_not_zero_and_full_artifacts_survive_source_deletion(self):
        identity = self.capture()
        record = self.store.get(identity)
        view = curator_view(record)
        self.assertIsNone(view["result"]["reward"])
        self.assertEqual(view["steps"][0]["observation"], '"full observation"')
        self.assertEqual(view["costs"]["usage_by_response"][0]["total_tokens"], 12)
        self.assertIsNone(view["costs"]["money"])
        (self.episode / "workspace/result.txt").unlink()
        self.assertEqual(self.store.artifact(record["artifacts"][0]["sha256"]), b"output")
        with self.store.connect() as db:
            db.execute("UPDATE episodes SET body=? WHERE id=?", ('{}', identity))
        with self.assertRaises(ValueError): self.store.get(identity)

    def test_original_and_reviewed_scores_are_preserved_but_prompt_contains_no_checks(self):
        identity = self.capture({"valid": True, "passed": True, "reward": 1, "verifier_version": "v1",
            "raw": {"original_evaluation": {"pass": False, "checks": "PRIVATE_REFERENCE_ANSWER"},
                    "reviewed_evaluation": {"pass": True}}})
        request = self.editor.prepare(identity, generator_policy_id="g1")
        self.assertEqual(request["view"]["result"]["reward"], 0)
        self.assertNotIn("PRIVATE_REFERENCE_ANSWER", json.dumps(request))
        self.assertIn("PRIVATE_REFERENCE_ANSWER", json.dumps(self.store.get(identity)))

    def test_compression_reports_loss_and_retains_last_observation(self):
        record = self.store.get(self.capture())
        record["trajectory"] += [{"role": "assistant", "text": "final answer", "calls": []}]
        view = curator_view(record, max_steps=1, observation_chars=5)
        self.assertEqual(view["steps"][0]["action"], "final_response")
        self.assertEqual(len(view["truncation"]), 2)
        self.assertIn("TRUNCATED", view["steps"][0]["observation"])

    def test_candidate_isolation_publish_cas_restart_and_retrieval(self):
        first = self.candidate()
        rival_request = self.editor.prepare(self.capture(), generator_policy_id="other-generator")
        rival = self.editor.apply(rival_request, json.dumps([{"name": "new_skill_insert", "arguments": {"skill_name": "Verify output", "content": CONTENT}}]))
        self.assertIsNone(self.library.head())
        self.assertEqual(self.library.retrieve("output"), [])
        version = self.library.publish(first["id"], expected_head=None)
        self.assertEqual(self.library.publish(first["id"], expected_head=None), version)
        with self.assertRaises(SkillConflict): self.library.publish(rival["id"], expected_head=None)
        reopened = SkillLibrary(self.library.root)
        self.assertEqual(reopened.retrieve("deliverables")[0]["name"], "Verify output")
        self.assertEqual(reopened.get(version)["Verify output"], CONTENT)

    def test_rename_delete_and_invalid_batch_are_atomic(self):
        first = self.candidate()
        request = self.editor.prepare(self.capture(), generator_policy_id="g1", parent=first["version"])
        response = json.dumps([{"name": "skill_update", "arguments": {"skill_name": "Verify output", "new_name": "Inspect output"}}])
        renamed = self.editor.apply(request, response)
        self.assertIn("name: Inspect output", self.library.get(renamed["version"])["Inspect output"])
        invalid = json.dumps([{"name": "skill_delete", "arguments": {"skill_name": "Verify output"}},
                              {"name": "skill_delete", "arguments": {"skill_name": "missing"}}])
        with self.assertRaises(ValueError): self.editor.apply(request, invalid)
        self.assertIn("Verify output", self.library.get(first["version"]))
        deleted = self.editor.apply(request, json.dumps([{"name": "skill_delete", "arguments": {"skill_name": "Verify output"}}]))
        self.assertEqual(self.library.get(deleted["version"]), {})
        for bad in ('{"name":"skill_delete"}', '[{"name":"skill_delete","name":"other","arguments":{}}]',
                    json.dumps([{"name": "new_skill_insert", "arguments": {"skill_name": "Mismatch", "content": CONTENT}}])):
            with self.assertRaises(ValueError): self.editor.apply(request, bad)

    def test_attribution_rejects_seed_wrong_executor_and_changed_skill(self):
        candidate = self.candidate()
        execution = self.store.get(self.capture({"valid": True, "passed": True, "reward": 1, "verifier_version": "v1"}))
        execution["executor"].update(fixed_executor_id="frozen", skill_version=candidate["version"])
        with self.assertRaises(ValueError): attribute(candidate, execution, fixed_executor_id="frozen")
        execution["task"].update(benchmark="another", task_id="task", uid="another:task")
        execution["receipt"].update(task_uid="another:task", task_sha256=digest(execution["task"]))
        self.assertEqual(attribute(candidate, execution, fixed_executor_id="frozen")["reward"], 1)
        execution["executor"]["skill_version"] = "different"
        with self.assertRaises(ValueError): attribute(candidate, execution, fixed_executor_id="frozen")

    def test_generator_transport_creates_candidate_without_training_admission(self):
        class Generator:
            async def turn(owned, system, tools, history):
                self.assertIn("Agent Trajectory", history[0]["content"])
                return {"calls": [{"id": "op", "name": "new_skill_insert", "arguments": {"skill_name": "Verify output", "content": CONTENT}}], "usage": {"total_tokens": 10}}
        result = asyncio.run(self.editor.generate(self.capture(), model=Generator(), generator_policy_id="g1"))
        self.assertFalse(result["training_admitted"])
        self.assertIsNone(self.library.head())

    def test_provider_namespace_alias_preserves_raw_reply_and_skill_bytes(self):
        request = self.editor.prepare(self.capture(), generator_policy_id="g1")
        raw = json.dumps([{"name": "functions.new_skill_insert", "arguments": {"skill_name": "Verify output", "content": CONTENT}}])
        candidate = self.editor.apply(request, raw, sampling={"reply": {"provider_response_id": "actual-provider-reference"}})
        provenance = candidate["provenance"]
        self.assertEqual(provenance["response"], raw)
        self.assertEqual(provenance["transport"]["aliases"][0]["canonical"], "new_skill_insert")
        self.assertEqual(self.library.get(candidate["version"])["Verify output"], CONTENT)
        for unknown in ("functions.run_shell", "functions.functions.new_skill_insert", "other.new_skill_insert"):
            with self.assertRaises(ValueError):
                self.editor.apply(request, json.dumps([{"name": unknown, "arguments": {"skill_name": "Verify output", "content": CONTENT}}]))

    def test_capture_failure_is_visible_without_rewriting_task_score(self):
        class Environment:
            def __init__(owned, **kwargs): pass
            async def initialize(owned): return {"image_id": "fixture"}
            def discover(owned): return [{"name": "write_file", "description": "Write", "parameters": {"type": "object"}}]
            async def call(owned, name, arguments): return {"ok": True}
            async def evaluate(owned): return {"pass": True}
            async def close(owned): return {"resources_closed": True}
        with patch.object(ExperienceStore, "capture", side_effect=OSError("Storage unavailable")):
            result = asyncio.run(APIExecutor(project_root=self.root, model_factory=Driver,
                environment_factory=Environment).execute_async(self.tasks, ExecutorOptions("openai", model="fixture")))
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertFalse(result["experience_complete"])
        self.assertEqual(result["results"][0]["evaluation"]["reward"], 1)
        self.assertEqual(result["results"][0]["experience_error"], "OSError")

    def test_executor_to_experience_skill_execution_and_downstream_attribution(self):
        systems = []
        class Model(Driver):
            async def turn(owned, system, tools, history):
                systems.append(system)
                return await super().turn(system, tools, history)
        class Environment:
            def __init__(owned, **kwargs):
                owned.workspace = kwargs["directory"] / "workspace"
            async def initialize(owned):
                owned.workspace.mkdir()
                return {"image_id": "fixture-image"}
            def discover(owned):
                return [{"name": "write_file", "description": "Write", "parameters": {"type": "object"}}]
            async def call(owned, name, arguments):
                (owned.workspace / arguments["path"]).write_text(arguments["content"])
                return {"ok": True}
            async def evaluate(owned): return {"pass": True}
            async def close(owned): return {"resources_closed": True}
        executor = APIExecutor(project_root=self.root, model_factory=Model, environment_factory=Environment)
        seed = asyncio.run(executor.execute_async(self.tasks, ExecutorOptions("openai", model="fixture")))
        seed_id = seed["results"][0]["experience"]["id"]
        candidate = self.candidate(seed_id)
        install(self.root, "downstream")
        downstream = load_task_set(TaskStore(self.root).register("downstream")["task_set"])
        receipt = asyncio.run(executor.execute_async(downstream, ExecutorOptions("openai", model="fixture", skill_version=candidate["version"])))
        episode = self.store.get(receipt["results"][0]["experience"]["id"])
        self.assertIn(CONTENT, systems[-1])
        attributed = attribute(candidate, episode, fixed_executor_id=episode["executor"]["fixed_executor_id"])
        self.assertEqual(attributed["reward"], 1)
        seed_episode = self.store.get(seed_id)
        self.assertEqual(seed_episode["executor"]["fixed_executor_id"], attributed["fixed_executor_id"])
        self.assertTrue(episode["calls"][0]["output"]["ok"])
        imported = ingest_run(self.store, Path(seed["receipt_path"]).parent)
        self.assertEqual(imported["imported"], 1)

    @unittest.skipUnless(os.environ.get("RUN_CONTAINER_CHECKS") == "1", "Set RUN_CONTAINER_CHECKS=1 for Docker")
    def test_real_docker_skill_candidate_execution_evidence_and_attribution(self):
        candidate = self.candidate()
        install(self.root, "downstream")
        tasks = load_task_set(TaskStore(self.root).register("downstream")["task_set"])
        result = asyncio.run(APIExecutor(project_root=self.root, model_factory=Driver).execute_async(
            tasks, ExecutorOptions("openai", model="fixture-driver", skill_version=candidate["version"], task_timeout=120)))
        row = result["results"][0]
        self.assertEqual(row["status"], "completed", row)
        self.assertTrue(row["resources_closed"])
        execution = self.store.get(row["experience"]["id"])
        self.assertEqual(self.store.artifact(next(a["sha256"] for a in execution["artifacts"] if a["path"] == "result.txt")), b"hello\n")
        self.assertEqual(attribute(candidate, execution, fixed_executor_id=execution["executor"]["fixed_executor_id"])["reward"], 1)


if __name__ == "__main__":
    unittest.main()
