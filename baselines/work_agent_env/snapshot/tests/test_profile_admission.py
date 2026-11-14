from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from evaluation.firewall import ContaminationError
from integrations.task_profiles import build_registry, ROOT
from training.profile_admission import validate_task_profile
from training.skill_contract import TrainingRejected
from tests.test_skill_generator_contract import firewall


class GeneratorProfileAdmissionTests(unittest.TestCase):
    def provenance(self):
        p = next(iter(build_registry(ROOT)["profiles"].values()))
        return {"source": "toolathlon_gym", "split": "train", "task_id": p["task_id"], "task_family": "unit-training-family",
                "source_commit": p["source_commit"], "seed_sha256": "a" * 64, "prompt_sha256": "b" * 64,
                "ancestor_tasks": [], "ancestor_families": []}

    def test_reviewed_fixture_profile_contract_is_not_model_execution_or_new_admission(self):
        provenance = self.provenance()
        shared = validate_task_profile(provenance, firewall(provenance), resource_backend="shared")
        isolated = validate_task_profile(provenance, firewall(provenance), resource_backend="isolated")
        self.assertEqual(shared["profile_sha256"], isolated["profile_sha256"])
        self.assertFalse(shared["runtime_scheduler_activation"])
        self.assertFalse(shared["training_admission"])

    def test_candidate_and_verified_records_are_rejected_before_profile_files_are_read(self):
        for change in ({"split": "candidate"}, {"source": "toolathlon_verified"}):
            provenance = {**self.provenance(), **change}
            with patch("training.profile_admission.read_owned") as reader:
                with self.assertRaises(ContaminationError): validate_task_profile(provenance, firewall(provenance))
                reader.assert_not_called()

    def test_reviewed_but_unsupported_task_source_or_backend_cannot_enter_dataset(self):
        for change, backend in (({"task_id": "unit-unsupported"}, "shared"), ({"source_commit": "f" * 40}, "shared"), ({}, "verified")):
            provenance = {**self.provenance(), **change}
            with self.assertRaises(TrainingRejected): validate_task_profile(provenance, firewall(provenance), resource_backend=backend)

    def test_rebound_tool_profile_is_rejected_even_for_reviewed_fixture_seed(self):
        registry = deepcopy(build_registry(ROOT))
        p = next(iter(registry["profiles"].values()))
        p["mcp_methods"]["woocommerce"].append("unit-unapproved-write")
        provenance = self.provenance()
        with patch("training.profile_admission.read_owned", return_value=json.dumps(registry).encode()):
            with self.assertRaises(TrainingRejected): validate_task_profile(provenance, firewall(provenance))
