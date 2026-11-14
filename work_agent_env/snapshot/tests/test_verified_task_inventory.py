from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from evaluation import result_import, verified_plan
from evaluation.firewall import digest
from evaluation.task_inventory import ROOT, INDEX_PATH, REQUEST_SCHEMA, load_index, requested_tasks, validate_index
from training.checkpoint_manifest import freeze


class VerifiedTaskInventoryTests(unittest.TestCase):
    def fixture(self):
        manifest = {"commit": "a" * 40, "task_ids": ["unit-task", "metadata.json"]}
        index = {"schema_version": "toolathlon-verified-task-index-v1", "source": "toolathlon_verified",
                 "commit": manifest["commit"], "evaluation_manifest_sha256": digest(manifest),
                 "root_git_tree": "b" * 40, "task_ids": ["unit-task"],
                 "non_task_metadata_objects": {"metadata.json": "c" * 40}, "task_bodies_loaded": False, "solutions_loaded": False}
        return manifest, index

    def request(self):
        index, checksum = load_index(ROOT)
        manifest_path = ROOT / "evaluation/toolathlon_verified.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return {"schema_version": REQUEST_SCHEMA, "purpose": "independent_evaluation_only", "upstream_commit": manifest["commit"],
                "created_at": "2026-10-16T00:00:00+00:00", "status": "prepared_not_executed", "scores": None,
                "solutions_loaded": False, "real_model_calls": 0, "checkpoint_descriptor_sha256": "a" * 64,
                "training_registry_sha256": hashlib.sha256((ROOT / "training/seed_registry.json").read_bytes()).hexdigest(),
                "task_ids": index["task_ids"][:1], "evaluation_task_index_sha256": checksum,
                "evaluation_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}

    def test_only_task_trees_can_be_selected_from_a_typed_inventory(self):
        manifest, index = self.fixture()
        self.assertTrue(validate_index(index, manifest))
        self.assertTrue(requested_tasks(["unit-task"], index))
        for tasks in (["metadata.json"], ["unit-task", "unit-task"], ["missing"], []):
            with self.assertRaises(ValueError): requested_tasks(tasks, index)

    def test_stale_overlapping_or_incomplete_type_partitions_are_rejected(self):
        manifest, base = self.fixture()
        for changes in ({"commit": "d" * 40}, {"task_ids": ["unit-task", "metadata.json"]},
                        {"non_task_metadata_objects": {}}, {"task_ids": ["unit-task", "unit-task"]}):
            index = {**deepcopy(base), **changes}
            with self.assertRaises(ValueError): validate_index(index, manifest)

    def test_prepare_rejects_actual_metadata_entry_before_opening_checkpoint(self):
        with self.assertRaisesRegex(ValueError, "metadata files are not tasks"):
            verified_plan.prepare(Path("nonexistent-unit-checkpoint.json"), ["task_conflict.json"])

    def test_import_rejects_metadata_before_reading_any_result_file(self):
        request = self.request()
        request["task_ids"] = ["task_conflict.json"]
        with TemporaryDirectory() as directory, patch.object(result_import, "read_measurement") as reader:
            with self.assertRaisesRegex(ValueError, "metadata files are not tasks"):
                result_import.collect(request, Path(directory))
            reader.assert_not_called()

    def test_import_rejects_changed_index_manifest_bytes_and_legacy_requests(self):
        original = self.request()
        with TemporaryDirectory() as directory:
            for changes in ({"evaluation_task_index_sha256": "0" * 64}, {"evaluation_manifest_sha256": "0" * 64},
                            {"schema_version": "toolathlon-verified-independent-request-v1"}):
                with self.assertRaises(ValueError): result_import.collect({**original, **changes}, Path(directory))

    def test_fixture_checkpoint_prepare_and_missing_results_remain_unexecuted_unknown(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (INDEX_PATH, "evaluation/toolathlon_verified.manifest.json", "training/seed_registry.json"):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            model = root / "unit-fixture-not-real-model"
            model.mkdir()
            (model / "fixture.bin").write_bytes(b"UNIT FIXTURE, NO MODEL EXECUTION")
            descriptor = root / "checkpoint.json"
            descriptor.write_text(json.dumps(freeze(model, root / "training/seed_registry.json", "unit-executor")), encoding="utf-8")
            index, checksum = load_index(root)
            with patch.object(verified_plan, "ROOT", root):
                output = verified_plan.prepare(descriptor, index["task_ids"][:1])
            request = json.loads((output / "request.json").read_text(encoding="utf-8"))
            self.assertEqual(request["schema_version"], REQUEST_SCHEMA)
            self.assertEqual(request["evaluation_task_index_sha256"], checksum)
            self.assertEqual(request["status"], "prepared_not_executed")
            self.assertIsNone(request["scores"])
            with patch.object(result_import, "ROOT", root): report = result_import.collect(request, root)
            self.assertEqual(report["unknown"], 1)
            self.assertIsNone(report["observed_success_rate_valid_only"])
            self.assertFalse(report["official_evaluation_claim"])
