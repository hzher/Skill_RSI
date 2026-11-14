import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from evaluation.result_import import ROOT, collect, read_measurement
from training.checkpoint_manifest import freeze, verify
from evaluation.task_inventory import load_index, REQUEST_SCHEMA


class CheckpointManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.model = self.root / "unit-model-fixture"
        self.model.mkdir()
        (self.model / "fixture.bin").write_bytes(b"UNIT FIXTURE, NOT A REAL MODEL")
        (self.model / "config.json").write_text('{"unit_fixture": true}')
        self.registry = self.root / "registry.json"
        self.registry.write_text('{"unit_fixture": true}')

    def test_weight_bytes_registry_and_executor_bound_without_loading_model(self):
        descriptor = freeze(self.model, self.registry, "unit-executor")
        registry_sha = hashlib.sha256(self.registry.read_bytes()).hexdigest()
        self.assertTrue(verify(descriptor, registry_sha))
        self.assertIn("not_model_loading", descriptor["validation_scope"])
        self.assertEqual(len(descriptor["files"]), 2)

    def test_changed_weight_or_registry_rejected(self):
        descriptor = freeze(self.model, self.registry, "unit-executor")
        with self.assertRaises(ValueError): verify(descriptor, "0" * 64)
        (self.model / "fixture.bin").write_bytes(b"CHANGED UNIT FIXTURE")
        with self.assertRaises(ValueError): verify(descriptor, descriptor["training_registry_sha256"])

    def test_empty_directory_and_missing_executor_not_frozen(self):
        with self.assertRaises(ValueError): freeze(self.model, self.registry, "")
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError): freeze(empty, self.registry, "unit-executor")


class OfficialResultImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "eval_res.json"

    def test_official_boolean_results_and_null_unknown_are_distinct(self):
        for value, status in ((True, "passed"), (False, "failed"), (None, "unknown"), (0, "unknown"), ("true", "unknown")):
            self.path.write_text(json.dumps({"pass": value}))
            with self.subTest(value=value): self.assertEqual(read_measurement(self.path, self.root)["status"], status)

    def test_missing_corrupt_and_infrastructure_false_remain_unknown(self):
        self.assertEqual(read_measurement(self.path, self.root)["status"], "unknown")
        for raw in ("{", '{"pass":false,"failure":"evaluation_error"}', '{"pass":false,"failure":"log_file_not_found"}'):
            self.path.write_text(raw)
            with self.subTest(raw=raw): self.assertEqual(read_measurement(self.path, self.root)["status"], "unknown")

    def test_fixture_import_never_claims_real_execution_or_official_score(self):
        manifest = json.loads((ROOT / "evaluation/toolathlon_verified.manifest.json").read_text(encoding="utf-8"))
        tasks = manifest["task_ids"][:2]
        destination = self.root / "finalpool" / tasks[0]
        destination.mkdir(parents=True)
        (destination / "eval_res.json").write_text('{"pass":true}')
        index, index_sha = load_index(ROOT)
        request = {"schema_version": REQUEST_SCHEMA, "purpose": "independent_evaluation_only",
                   "created_at": "2026-10-16T00:00:00+00:00", "status": "prepared_not_executed", "scores": None,
                   "solutions_loaded": False, "real_model_calls": 0,
                   "training_registry_sha256": hashlib.sha256((ROOT / "training/seed_registry.json").read_bytes()).hexdigest(),
                   "evaluation_task_index_sha256": index_sha,
                   "evaluation_manifest_sha256": hashlib.sha256((ROOT / "evaluation/toolathlon_verified.manifest.json").read_bytes()).hexdigest(),
                   "upstream_commit": manifest["commit"], "task_ids": tasks, "checkpoint_descriptor_sha256": "a" * 64}
        result = collect(request, self.root)
        self.assertEqual((result["observed_passed"], result["observed_failed"], result["unknown"]), (1, 0, 1))
        self.assertEqual(result["execution_kind"], "offline_import_no_execution")
        self.assertFalse(result["official_evaluation_claim"])
        self.assertEqual(result["model_calls_performed_by_import"], 0)


if __name__ == "__main__": unittest.main()
