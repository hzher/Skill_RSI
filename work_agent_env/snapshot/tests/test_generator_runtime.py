"""Unit fixtures for skill handoff; no models are invoked or samples admitted."""
from copy import deepcopy
import json
import unittest

from training.generator_runtime import run_generated_skill
from training.skill_contract import TrainingRejected
from tests.test_skill_generator_contract import fixture


class FixtureExecutor:
    executor_id = "executor-unit"

    def __init__(self, measurement):
        self.measurement, self.seen = measurement, None

    async def execute(self, *, skill, task, resources):
        self.seen = {"skill": skill, "task": task, "resources": resources}
        return self.measurement

    async def invalid_skill_measurement(self, *, task, resources):
        return self.measurement


class GeneratorRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.skill = {"schema_version": "skill-v1", "skill_id": "fixture-only", "objective": "A fixture skill",
                      "steps": ["Read the task data", "Create the requested artifact"], "allowed_tools": ["unit_read"]}
        self.response = {"id": "fixture-only", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(self.skill)}], "prompt_token_ids": [11, 12],
            "generation_token_ids": [31, 32], "generation_log_probs": [-.1, -.2]}]}

    async def invoke(self, executor, response=None):
        row = fixture()
        return await run_generated_skill(model_response=response or self.response,
            downstream_task={"prompt": "unit downstream task"},
            provenance=row["provenance"], downstream_provenance=row["downstream_execution"]["provenance"],
            identities=row["identities"], executor=executor, resources="fixture-resource",
            capture_key=b"unit-fixture-key" * 4, global_step=0, provider_request_id="fixture-only",
            capture_kind="unit_fixture", training_run_id="unit-run", episode_id={"rollout_id": "unit-only", "attempt": 0})

    async def test_exact_generated_skill_consumed_by_fixed_executor(self):
        executor = FixtureExecutor(fixture()["measurement"])
        result = await self.invoke(executor)
        self.assertEqual(executor.seen["skill"], self.skill)
        self.assertEqual(executor.seen["task"]["prompt"], "unit downstream task")
        self.assertFalse(result["training_eligible"])
        record = result["skill_capture"]["record"]
        self.assertEqual(record["message_log"][-1]["actor"], "generator")
        self.assertNotIn("executor", json.dumps(record["message_log"]))
        self.assertEqual(record["downstream_execution"]["task_id"], "unit-downstream")

    async def test_unknown_resource_result_has_no_training_capture(self):
        executor = FixtureExecutor({**fixture()["measurement"], "reward": None, "mask_sample": True, "evaluation_status": "unknown"})
        result = await self.invoke(executor)
        self.assertIsNone(result["skill_capture"])
        self.assertFalse(result["training_eligible"])

    async def test_executor_version_mismatch_stops_before_use(self):
        executor = FixtureExecutor(fixture()["measurement"])
        executor.executor_id = "different-executor"
        with self.assertRaises(TrainingRejected): await self.invoke(executor)
        self.assertIsNone(executor.seen)

    async def test_missing_capture_fields_stop_before_executor_use(self):
        for key in ("prompt_token_ids", "generation_token_ids", "generation_log_probs"):
            response = deepcopy(self.response)
            del response["output"][0][key]
            executor = FixtureExecutor(fixture()["measurement"])
            with self.subTest(key=key), self.assertRaises(TrainingRejected): await self.invoke(executor, response)
            self.assertIsNone(executor.seen)

    async def test_generated_tool_call_cannot_be_misread_as_skill(self):
        response = deepcopy(self.response)
        response["output"][0]["type"] = "function_call"
        executor = FixtureExecutor(fixture()["measurement"])
        with self.assertRaises(TrainingRejected): await self.invoke(executor, response)
        self.assertIsNone(executor.seen)

    async def test_executor_cannot_mutate_the_signed_generator_skill(self):
        class MutatingFixtureExecutor(FixtureExecutor):
            async def execute(self, *, skill, task, resources):
                skill["steps"].append("Unit-only internal executor mutation")
                return self.measurement
        executor = MutatingFixtureExecutor(fixture()["measurement"])
        result = await self.invoke(executor)
        self.assertEqual(result["skill"], self.skill)
        self.assertEqual(result["skill_capture"]["record"]["generated_skill"], self.skill)

    async def test_original_generator_sampling_is_frozen_while_executor_runs(self):
        response = deepcopy(self.response)
        class CallerMutationExecutor(FixtureExecutor):
            async def execute(inner, **kwargs):
                response["output"][0]["generation_token_ids"][0] = 999
                return inner.measurement
        result = await self.invoke(CallerMutationExecutor(fixture()["measurement"]), response)
        self.assertEqual(result["skill_capture"]["record"]["message_log"][-1]["token_ids"], [31, 32])

    async def test_executor_version_change_during_downstream_run_is_rejected(self):
        class ChangingExecutor(FixtureExecutor):
            async def execute(inner, **kwargs):
                inner.executor_id = "changed-mid-execution"
                return inner.measurement
        with self.assertRaises(TrainingRejected): await self.invoke(ChangingExecutor(fixture()["measurement"]))

    async def test_invalid_sampled_json_gets_verified_failure_without_executor_use(self):
        response = deepcopy(self.response)
        response["output"][0]["content"][0]["text"] = "not valid JSON"
        executor = FixtureExecutor({**fixture()["measurement"], "reward": 0.0})
        result = await self.invoke(executor, response)
        self.assertIsNone(executor.seen)
        self.assertEqual(result["measurement"]["reward"], 0.0)
        record = result["skill_capture"]["record"]
        self.assertEqual(record["generation_status"], "invalid_skill")
        self.assertEqual(record["message_log"][-1]["token_ids"], [31, 32])
        self.assertFalse(result["training_eligible"])

    async def test_invalid_skill_with_unknown_verifier_has_no_capture(self):
        response = deepcopy(self.response)
        response["output"][0]["content"][0]["text"] = "{}"
        executor = FixtureExecutor({**fixture()["measurement"], "reward": None, "mask_sample": True})
        result = await self.invoke(executor, response)
        self.assertIsNone(result["skill_capture"])
        self.assertIsNone(result["measurement"]["reward"])
        self.assertTrue(result["measurement"]["mask_sample"])

    async def test_invalid_skill_cannot_receive_reward_for_precompleted_seed(self):
        response = deepcopy(self.response)
        response["output"][0]["content"][0]["text"] = "{}"
        result = await self.invoke(FixtureExecutor(fixture()["measurement"]), response)
        self.assertIsNone(result["skill_capture"])
        self.assertTrue(result["measurement"]["mask_sample"])


if __name__ == "__main__": unittest.main()
