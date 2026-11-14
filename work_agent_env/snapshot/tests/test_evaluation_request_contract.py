from copy import deepcopy
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from evaluation import result_import
from evaluation.request_contract import validate_request
from evaluation.task_inventory import ROOT, INDEX_PATH
import tests.test_verified_task_inventory as inventory_fixtures


class EvaluationRequestContractTests(unittest.TestCase):
    def request(self): return inventory_fixtures.VerifiedTaskInventoryTests().request()

    def test_missing_identity_or_false_execution_claim_is_rejected(self):
        original = self.request()
        variants = [{**original, **change} for change in ({"checkpoint_descriptor_sha256": "unit-not-a-digest"},
                    {"real_model_calls": False}, {"real_model_calls": 1}, {"scores": [1]},
                    {"solutions_loaded": True}, {"status": "completed"}, {"answer_material": "unit-only"})]
        missing = deepcopy(original)
        missing.pop("checkpoint_descriptor_sha256")
        variants.append(missing)
        for request in variants:
            with self.assertRaises(ValueError): validate_request(request, ROOT)

    def test_creation_time_requires_timezone_but_does_not_claim_request_authentication(self):
        request = self.request()
        self.assertFalse(validate_request(request, ROOT)["model_execution_verified"])
        for value in (None, "not-a-timestamp", "2026-10-16T00:00:00"):
            with self.assertRaises(ValueError): validate_request({**request, "created_at": value}, ROOT)

    def test_changed_training_registry_invalidates_request_before_reading_results(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (INDEX_PATH, "evaluation/toolathlon_verified.manifest.json", "training/seed_registry.json"):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            request = self.request()
            self.assertFalse(validate_request(request, root)["model_execution_verified"])
            with (root / "training/seed_registry.json").open("a", encoding="utf-8") as stream: stream.write("\n")
            with patch.object(result_import, "ROOT", root), patch.object(result_import, "read_measurement") as reader:
                with self.assertRaisesRegex(ValueError, "training registry bytes changed"):
                    result_import.collect(request, root)
                reader.assert_not_called()

    def test_partial_fixture_results_report_coverage_separately_from_valid_only_rate(self):
        request = self.request()
        index = json.loads((ROOT / INDEX_PATH).read_text(encoding="utf-8"))
        request["task_ids"] = index["task_ids"][:2]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "finalpool" / request["task_ids"][0] / "eval_res.json"
            result.parent.mkdir(parents=True)
            result.write_text('{"pass":true}')
            report = result_import.collect(request, root)
            self.assertEqual(report["observed_success_rate_valid_only"], 1)
            self.assertEqual(report["observed_valid_coverage"], 0.5)
            self.assertFalse(report["complete_result_coverage"])
            self.assertFalse(report["official_evaluation_claim"])
            self.assertFalse(report["request_validation"]["model_execution_verified"])
