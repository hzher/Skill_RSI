"""Mixed-task summaries cannot replace raw NeMo task/episode/result bindings."""
from copy import deepcopy
import unittest

from integrations.nemo_mixed_audit import validate_measurements


class MixedTaskAuditTests(unittest.TestCase):
    def setUp(self):
        self.seeds = [{"resources_session_id": str(i), "episode_id": {"rollout_id": "fixture-" + str(i)},
            "task_id": {"taskset": "toolathlon_gym:fixture", "task_id": "tax" if i == 0 else "meal"}} for i in range(3)]
        self.measured = [{"task_index": i, "result": {"reward": reward, "mask_sample": False, "fixture_sequence": sequence}}
            for sequence, (i, reward) in enumerate(((0, 0), (1, 0), (0, 1), (1, 1), (1, 1), (2, 0), (1, 1)))]
        self.journal = []
        for i, seed in enumerate(self.seeds):
            for m in self.measured:
                if m["task_index"] == i:
                    binding = {key: deepcopy(seed[key]) for key in ("task_id", "episode_id")}
                    self.journal.append({"route": "/isolated/verify", "status": 200,
                        "request": {**deepcopy(binding), "verification_input": {"resources_session_id": seed["resources_session_id"]}},
                        "response": {**deepcopy(binding), "result": deepcopy(m["result"])}})

    def test_interleaved_measurements_match_journals_grouped_by_cookie_client(self):
        validate_measurements(self.measured, self.journal, self.seeds)

    def test_rebound_task_or_episode_is_rejected(self):
        for field in ("task_id", "episode_id"):
            changed = deepcopy(self.journal)
            changed[2]["response"][field] = {"other": "binding"}
            with self.subTest(field=field), self.assertRaises(ValueError): validate_measurements(self.measured, changed, self.seeds)

    def test_forged_or_missing_raw_measurement_is_rejected(self):
        changed = deepcopy(self.journal)
        changed[0]["response"]["result"]["mask_sample"] = True
        with self.assertRaises(ValueError): validate_measurements(self.measured, changed, self.seeds)
        with self.assertRaises(ValueError): validate_measurements(self.measured, self.journal[:-1], self.seeds)
        # A boolean is not a numeric reward even though Python equates True/1.
        measured, journal = deepcopy(self.measured), deepcopy(self.journal)
        measured[2]["result"]["reward"] = True
        journal[1]["response"]["result"]["reward"] = True
        with self.assertRaises(ValueError): validate_measurements(measured, journal, self.seeds)
