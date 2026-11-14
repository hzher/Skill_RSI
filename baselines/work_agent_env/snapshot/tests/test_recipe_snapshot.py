"""Public recipe assets remain immutable, bounded and distinct from task answers."""
import json
from pathlib import Path
import tempfile
import unittest

from training.recipe_snapshot import inspect_recipe_data, retain_recipe_data, verify_snapshot
from training.task_lifecycle_design import PUBLIC_RECIPE_MEMBER, pinned_sources


class RecipeSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "configs").mkdir()
        (self.root / "configs/mainline_sources.lock.json").write_text(json.dumps({"sources": {"toolathlon_gym": {"commit": "a" * 40}}}))
        self.raw = json.dumps([{"id": "recipe-1", "name": "Unit recipe", "category": "unit"}]).encode()

    def receipt(self):
        summary = retain_recipe_data(self.root, self.raw)
        return {**summary, "source_commit": "a" * 40, "source_object": {"member": PUBLIC_RECIPE_MEMBER,
                "sha256": summary["sha256"], "git_blob": summary["git_blob"]}}

    def test_same_public_asset_reuses_original_bytes(self):
        receipt = self.receipt()
        self.assertEqual(retain_recipe_data(self.root, self.raw)["asset_ref"], receipt["asset_ref"])
        self.assertEqual(verify_snapshot(self.root, receipt)["recipe_count"], 1)
        self.assertEqual(len(list((self.root / receipt["asset_ref"]).parent.iterdir())), 1)

    def test_tampered_asset_is_rejected_and_not_replaced(self):
        receipt = self.receipt()
        path = self.root / receipt["asset_ref"]
        path.write_bytes(b"broken")
        with self.assertRaises(ValueError): verify_snapshot(self.root, receipt)
        with self.assertRaises(ValueError): retain_recipe_data(self.root, self.raw)
        self.assertEqual(path.read_bytes(), b"broken")

    def test_foreign_source_path_and_duplicate_recipe_ids_are_rejected(self):
        receipt = self.receipt()
        receipt["source_commit"] = "b" * 40
        with self.assertRaises(ValueError): verify_snapshot(self.root, receipt)
        row = {"id": "duplicate", "name": "fixture", "category": "unit"}
        with self.assertRaises(ValueError): inspect_recipe_data(json.dumps([row, row]).encode())
        with self.assertRaises(ValueError): inspect_recipe_data(b"x" * (2 * 1024 * 1024 + 1))

    def test_reader_rejects_answer_paths_before_invoking_git(self):
        with self.assertRaises(ValueError):
            pinned_sources(self.root, {}, members={"answer": "tasks/finalpool/unit/groundtruth_workspace/answer.xlsx"})
