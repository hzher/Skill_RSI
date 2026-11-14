from copy import deepcopy
import unittest

from training.seed_contract import prompt_digest, validate_seed_row
from training.skill_contract import TrainingRejected
from tests.test_skill_generator_contract import fixture, firewall


def seed():
    row = {"training_target": "skill_generator", "agent_ref": {"type": "responses_api_agents", "name": "skill_generator"},
           "task_prompt": "Unit task only", "responses_create_params": {"input": [{"role": "user", "content": "Generate a declarative unit skill"}]},
           "provenance": fixture()["provenance"],
           "downstream_task_prompt": "New unit task",
           "downstream_provenance": fixture()["downstream_execution"]["provenance"]}
    row["provenance"]["prompt_sha256"] = prompt_digest(row["task_prompt"], row["responses_create_params"])
    row["downstream_provenance"]["prompt_sha256"] = prompt_digest(row["downstream_task_prompt"],
        {"input": [{"role": "user", "content": row["downstream_task_prompt"]}]})
    return row


class SeedContractTests(unittest.TestCase):
    def test_reviewed_seed_and_sampling_changes_preserve_prompt_binding(self):
        row = seed()
        guard = firewall(deepcopy(row["provenance"]), deepcopy(row["downstream_provenance"]))
        self.assertEqual(validate_seed_row(row, guard), row)
        row["responses_create_params"].update(temperature=.7, top_p=.9, model="actual-policy")
        self.assertEqual(validate_seed_row(row, guard), row)

    def test_changed_task_or_generator_prompt_rejected(self):
        for task in (True, False):
            row = seed()
            guard = firewall(deepcopy(row["provenance"]), deepcopy(row["downstream_provenance"]))
            if task: row["task_prompt"] = "Changed task"
            else: row["responses_create_params"]["input"][0]["content"] = "Unreviewed prompt"
            with self.subTest(task=task), self.assertRaises(TrainingRejected): validate_seed_row(row, guard)

    def test_downstream_task_must_be_independently_reviewed_and_distinct(self):
        row = seed()
        guard = firewall(deepcopy(row["provenance"]), deepcopy(row["downstream_provenance"]))
        row["downstream_task_prompt"] = "Changed downstream task"
        with self.assertRaises(TrainingRejected): validate_seed_row(row, guard)
        row = seed()
        row["downstream_provenance"] = deepcopy(row["provenance"])
        with self.assertRaises(TrainingRejected): validate_seed_row(row, guard)
        row = seed()
        with self.assertRaises(ValueError): validate_seed_row(row, firewall(deepcopy(row["provenance"])))

    def test_synthetic_assistant_answer_and_tools_cannot_enter_seed(self):
        row = seed()
        guard = firewall(deepcopy(row["provenance"]), deepcopy(row["downstream_provenance"]))
        row["responses_create_params"]["input"][0]["role"] = "assistant"
        with self.assertRaises(TrainingRejected): validate_seed_row(row, guard)
        row = seed()
        row["responses_create_params"]["tools"] = [{"name": "read_private_evaluation"}]
        with self.assertRaises(TrainingRejected): validate_seed_row(row, guard)

    def test_extra_answer_fields_and_routing_override_are_rejected(self):
        row = seed()
        guard = firewall(deepcopy(row["provenance"]))
        row["reference_solution"] = "unit forbidden field"
        with self.assertRaises(TrainingRejected): validate_seed_row(row, guard)
        row = seed()
        row["agent_ref"]["name"] = "executor"
        with self.assertRaises(TrainingRejected): validate_seed_row(row, guard)

    def test_nemo_default_message_type_is_semantically_normalized(self):
        row = seed()
        before = row["provenance"]["prompt_sha256"]
        row["responses_create_params"]["input"][0]["type"] = "message"
        self.assertEqual(before, prompt_digest(row["task_prompt"], row["responses_create_params"]))


if __name__ == "__main__": unittest.main()
