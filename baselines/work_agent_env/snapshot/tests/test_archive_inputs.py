"""Structured audit authorities retain every raw-data freshness requirement."""
from copy import deepcopy
import hashlib
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations import archive_inputs as ai, evidence_archive as ea
from integrations.mainline_audit import collect_inputs, inputs_current, read_json, track_input, track_absence


class ArchiveInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.name = "runs/unit-proof/input.json"
        self.original = self.root / self.name
        self.original.parent.mkdir(parents=True)
        self.original.write_bytes(b'{"value":1}\n')
        self.source = self.root / "integrations/unit.py"
        self.source.parent.mkdir()
        self.source.write_bytes(b"UNIT = True\n")
        hashes = {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (self.original, self.source)}
        self.raw = {"schema_version": "mainline-consistency-audit-v1", "passed": True,
                    "checks": [{"name": "unit", "passed": True, "detail": {"reward": None}}],
                    "input_sha256": hashes, "expected_absent": []}
        path = self.root / ea.AUDIT
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.raw), encoding="utf-8")
        self.ref = ea.create(self.root)["archive_ref"]
        self.grouped = self.collect()
        self.summary = {"tasks": 2, "training_admissions": 0}

    def collect(self, *, missing=None):
        with collect_inputs(self.root, archive_ref=self.ref) as state:
            read_json(self.original, self.root)
            track_input(self.source, self.root)
            if missing is not None: track_absence(missing, self.root)
        result = {**self.raw, "input_sha256": state["files"], "expected_absent": sorted(state["missing"]),
                  **ai.fields(self.root, state["files"], state["archive"], state["archived"])}
        result["checks"] = [*self.raw["checks"], {"name": ai.CHECK, "passed": True}]
        return result

    def test_direct_bindings_shrink_but_all_original_bytes_remain_required(self):
        self.assertNotIn(self.name, self.grouped["input_sha256"])
        self.assertIn(self.ref, self.grouped["input_sha256"])
        self.assertEqual(self.grouped["archive_inputs"]["used_members"], [self.name])
        self.assertTrue(inputs_current(self.root, self.grouped))
        self.assertTrue(inputs_current(self.root, self.raw))
        self.assertEqual(ai.logical_inputs(self.grouped["input_sha256"], ai.load(self.root, self.ref), [self.name]), self.raw["input_sha256"])

    def test_archive_metadata_and_all_expanded_members_count_toward_logical_bytes(self):
        authority = ai.load(self.root, self.ref)
        expected = len(self.original.read_bytes()) + len(self.source.read_bytes()) + authority["metadata_bytes"]
        self.assertEqual(self.grouped["input_budget"]["logical_bytes"], expected)
        self.assertEqual(self.grouped["input_budget"]["logical_files"], 2)
        self.assertEqual(self.grouped["input_budget"]["authority_files"], 2)

    def test_mutated_or_missing_original_invalidates_grouped_audit(self):
        self.original.write_bytes(b'{"value":2}\n')
        self.assertFalse(inputs_current(self.root, self.grouped))
        self.original.unlink()
        self.assertFalse(inputs_current(self.root, self.grouped))

    def test_archive_corruption_or_loss_does_not_fall_back_to_raw_mode(self):
        path = self.root / self.ref
        path.write_bytes(b"not a zip")
        self.assertFalse(inputs_current(self.root, self.grouped))
        path.unlink()
        self.assertFalse(inputs_current(self.root, self.grouped))

    def test_source_bytes_are_still_direct_and_current(self):
        self.source.write_bytes(b"UNIT = None\n")
        self.assertFalse(inputs_current(self.root, self.grouped))

    def test_absence_is_not_satisfied_by_an_archived_old_state(self):
        missing = self.root / "runs/unit-proof/stopped.pid"
        receipt = self.collect(missing=missing)
        self.assertTrue(inputs_current(self.root, receipt))
        missing.write_bytes(b"unexpected process")
        self.assertFalse(inputs_current(self.root, receipt))

    def test_unknown_accounting_and_forged_budget_are_rejected(self):
        for field in ("logical_bytes", "authority_files", "archived_members"):
            receipt = deepcopy(self.grouped)
            receipt["input_budget"][field] = 0
            with self.subTest(field=field): self.assertFalse(inputs_current(self.root, receipt))
        receipt = deepcopy(self.grouped)
        receipt["archive_inputs"]["accounting"] = "unbounded"
        self.assertFalse(inputs_current(self.root, receipt))
        receipt = deepcopy(self.grouped)
        receipt["input_budget"]["archived_members"] = True
        self.assertFalse(inputs_current(self.root, receipt))

    def test_omitted_duplicated_or_foreign_used_members_are_rejected(self):
        for members in ([], [self.name, self.name], ["runs/foreign/input.json"], [None]):
            receipt = deepcopy(self.grouped)
            receipt["archive_inputs"]["used_members"] = members
            with self.subTest(members=members): self.assertFalse(inputs_current(self.root, receipt))

    def test_direct_archive_overlap_is_rejected(self):
        receipt = deepcopy(self.grouped)
        receipt["input_sha256"][self.name] = self.raw["input_sha256"][self.name]
        self.assertFalse(inputs_current(self.root, receipt))

    def test_authority_sha_is_mandatory(self):
        receipt = deepcopy(self.grouped)
        receipt["input_sha256"][self.ref] = "0" * 64
        self.assertFalse(inputs_current(self.root, receipt))

    def test_unknown_archive_fields_cannot_be_ignored_by_legacy_receipts(self):
        receipt = deepcopy(self.raw)
        receipt["input_budget"] = self.grouped["input_budget"]
        self.assertFalse(inputs_current(self.root, receipt))
        stripped = {key: value for key, value in self.grouped.items()
                    if key not in {"archive_inputs", "input_budget", "logical_input_manifest_sha256"}}
        self.assertFalse(inputs_current(self.root, stripped))
        with self.assertRaises(ValueError):
            with collect_inputs(self.root, archive_ref=""): pass

    def test_change_during_collection_and_wrong_supplied_digest_are_rejected(self):
        with collect_inputs(self.root, archive_ref=self.ref):
            read_json(self.original, self.root)
            self.original.write_bytes(b'{"value":2}\n')
            with self.assertRaisesRegex(ValueError, "changed during collection"): read_json(self.original, self.root)
        self.original.write_bytes(b'{"value":1}\n')
        with collect_inputs(self.root, archive_ref=self.ref):
            with self.assertRaisesRegex(ValueError, "Archived original changed"):
                track_input(self.original, self.root, "0" * 64)

    def test_logical_budget_is_not_compressed_size(self):
        with patch.object(ea, "MAX_CONTENT", self.grouped["input_budget"]["logical_bytes"] - 1):
            self.assertFalse(inputs_current(self.root, self.grouped))

    def test_authority_and_member_counts_are_independently_bounded(self):
        authority = ai.load(self.root, self.ref)
        with self.assertRaises(ValueError): ai.budget(self.root, {str(i): "0" * 64 for i in range(4097)}, authority)
        authority["members"] = {"runs/unit/" + str(i) + ".json": {} for i in range(4097)}
        with self.assertRaises(ValueError): ai.budget(self.root, self.grouped["input_sha256"], authority)

    def test_equivalent_modes_can_be_compared_without_touching_originals(self):
        result = ai.compare(self.root, self.raw, self.grouped, self.summary, self.summary)
        self.assertTrue(result["passed"])
        self.assertEqual(result["matched_logical_inputs"], 2)
        self.assertEqual(result["matched_existing_checks"], 1)
        self.assertTrue(inputs_current(self.root, self.raw))
        self.assertTrue((self.root / result["raw_audit_ref"]).is_file())

    def test_changed_conclusions_inputs_or_task_qualification_block_activation(self):
        changed = deepcopy(self.grouped)
        changed["checks"][0]["detail"]["reward"] = 0
        with self.assertRaisesRegex(ValueError, "conclusions differ"):
            ai.compare(self.root, self.raw, changed, self.summary, self.summary)
        with self.assertRaisesRegex(ValueError, "qualification differ"):
            ai.compare(self.root, self.raw, self.grouped, self.summary, {"tasks": 7})
        self.assertFalse((self.root / ai.POLICY).exists())

    def test_activation_changes_policy_and_invalidates_old_absence(self):
        grouped = self.collect(missing=self.root / ai.POLICY)
        raw = {**self.raw, "expected_absent": [ai.POLICY]}
        comparison = ai.compare(self.root, raw, grouped, self.summary, self.summary)
        policy = ai.activate(self.root, grouped, comparison)
        self.assertEqual(ai.read_policy(self.root), policy)
        self.assertFalse(inputs_current(self.root, grouped))
        self.assertEqual(self.original.read_bytes(), b'{"value":1}\n')

    def test_foreign_or_corrupt_policy_and_comparison_refuse_loading(self):
        result = ai.compare(self.root, self.raw, self.grouped, self.summary, self.summary)
        policy = ai.activate(self.root, self.grouped, result)
        comparison = self.root / policy["comparison_ref"]
        comparison.write_bytes(b"{}")
        with self.assertRaises(ValueError): ai.read_policy(self.root)
        (self.root / ai.POLICY).write_bytes(b"{}")
        with self.assertRaises(ValueError): ai.read_policy(self.root)

    def test_archive_copy_creation_requires_flat_audit_not_nested_authorities(self):
        (self.root / ea.AUDIT).write_text(json.dumps(self.grouped))
        with self.assertRaisesRegex(ValueError, "current passing"): ea.create(self.root)

    def test_copying_private_state_to_another_workspace_cannot_reuse_current_policy(self):
        comparison = ai.compare(self.root, self.raw, self.grouped, self.summary, self.summary)
        policy = ai.activate(self.root, self.grouped, comparison)
        with collect_inputs(self.root, archive_ref=self.ref) as state:
            read_json(self.original, self.root)
            track_input(self.source, self.root)
            read_json(self.root / ai.POLICY, self.root)
            read_json(self.root / policy["comparison_ref"], self.root)
        receipt = {**self.grouped, "input_sha256": state["files"],
                   **ai.fields(self.root, state["files"], state["archive"], state["archived"])}
        self.assertTrue(inputs_current(self.root, receipt))
        with tempfile.TemporaryDirectory() as other:
            clone = Path(other) / "copied-project"
            shutil.copytree(self.root, clone)
            self.assertFalse(inputs_current(clone, receipt))


if __name__ == "__main__": unittest.main()
