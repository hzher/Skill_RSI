"""Synthetic envelopes test the handoff parser; no real training is performed."""
from copy import deepcopy
import hashlib
import unittest

from tests.test_skill_generator_contract import fixture, firewall
from training.rollout_handoff import prepare_rollout_batch
from training.skill_contract import TrainingRejected, sign_capture


KEY = b"fixture-handoff-key" * 3


def example(index=0, reward=1):
    record = fixture()
    task_prompt = "Unit downstream task prompt"
    record["downstream_execution"]["task_prompt_sha256"] = hashlib.sha256(task_prompt.encode()).hexdigest()
    record["measurement"]["reward"] = reward
    record["capture"].update(kind="provider_response", model_requests=2,
        capture_id=f"fixture-capture-{index}", global_step=3,
        training_run_id="fixture-run", episode_id={"rollout_id": f"fixture-{index}", "attempt": 0})
    request = {"provenance": deepcopy(record["provenance"]),
               "downstream_provenance": deepcopy(record["downstream_execution"]["provenance"]),
               "downstream_task_prompt": task_prompt,
               "episode_id": deepcopy(record["capture"]["episode_id"])}
    return request, {"record": record, "signature": sign_capture(record, KEY)}


class RolloutHandoffTests(unittest.TestCase):
    def prepare(self, requests, captures):
        record = fixture()
        guard = firewall(record["provenance"], record["downstream_execution"]["provenance"])
        return prepare_rollout_batch(requests=requests, captures=captures,
            capture_key=KEY, firewall=guard, generator_policy_id="generator-unit",
            fixed_executor_id="executor-unit", training_run_id="fixture-run", global_step=3)

    def test_completed_multi_turn_samples_keep_generator_tokens_and_downstream_reward(self):
        request0, capture0 = example(0, reward=0)
        request1, capture1 = example(1, reward=1)
        payloads = self.prepare([request0, request1], [capture0, capture1])
        self.assertEqual([p["full_result"]["reward"] for p in payloads], [0, 1])
        self.assertEqual(payloads[0]["message_log"][2]["role"], "tool")
        self.assertEqual(payloads[0]["message_log"][3]["generation_logprobs"], [-.3])
        self.assertEqual(payloads[0]["downstream_execution"]["task_id"], "unit-downstream")
        self.assertNotIn("executor", str(payloads[0]["message_log"]))

    def test_different_downstream_task_cannot_supply_the_reward(self):
        request, capture = example()
        request["downstream_provenance"]["task_id"] = "different-task"
        with self.assertRaises(TrainingRejected): self.prepare([request], [capture])
        request, capture = example()
        request["downstream_task_prompt"] = "Changed public task prompt"
        with self.assertRaises(TrainingRejected): self.prepare([request], [capture])

    def test_stale_policy_step_executor_and_run_are_rejected(self):
        for section, field, value in (("capture", "global_step", 2),
                                      ("capture", "training_run_id", "previous-run"),
                                      ("identities", "fixed_executor_id", "other-executor"),
                                      ("identities", "generator_policy_id", "other-policy")):
            request, capture = example()
            capture["record"][section][field] = value
            capture["signature"] = sign_capture(capture["record"], KEY)
            with self.subTest(field=field), self.assertRaises(TrainingRejected):
                self.prepare([request], [capture])

    def test_replayed_or_missing_samples_cannot_form_a_batch(self):
        request, capture = example()
        with self.assertRaises(TrainingRejected): self.prepare([request, request], [capture, capture])
        with self.assertRaises(TrainingRejected): self.prepare([request], [])
        with self.assertRaises(TrainingRejected): self.prepare([request], [None])

    def test_unknown_measurement_and_changed_signed_reward_are_rejected(self):
        request, capture = example()
        capture["record"]["measurement"].update(reward=None, mask_sample=True, evaluation_status="unknown")
        capture["signature"] = sign_capture(capture["record"], KEY)
        with self.assertRaises(TrainingRejected): self.prepare([request], [capture])
        request, capture = example()
        capture["record"]["measurement"]["reward"] = 0
        with self.assertRaisesRegex(TrainingRejected, "authentication"):
            self.prepare([request], [capture])


if __name__ == "__main__":
    unittest.main()
