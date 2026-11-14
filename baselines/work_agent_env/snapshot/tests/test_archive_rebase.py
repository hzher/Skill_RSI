"""Temporary source/audit fixtures; no task or runtime acceptance is inferred."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from integrations import archive_inputs as ai, evidence_archive as ea
from integrations.mainline_audit import collect_inputs, inputs_current, read_json, track_input


class ArchiveRebaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="archive-rebase-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.names = ["runs/unit-proof/current.json", "runs/unit-proof/obsolete.json"]
        self.source = "integrations/unit.py"
        for name in (*self.names, self.source):
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'{"explicit_unit_fixture":true}\n' if name.endswith(".json") else b"# explicit unit fixture\n")
        self.raw = {"schema_version": "mainline-consistency-audit-v1", "passed": True,
            "checks": [{"name": "unit", "passed": True, "detail": {"reward": None}}], "expected_absent": [],
            "input_sha256": {n: hashlib.sha256((self.root/n).read_bytes()).hexdigest() for n in (*self.names, self.source)}}
        self.audit_path = self.root/ea.AUDIT
        self.audit_path.parent.mkdir(parents=True)
        self.audit_path.write_text(json.dumps(self.raw))
        self.previous = ea.create(self.root)["archive_ref"]
        self.ref = self.previous
        initial = self.collect(all_members=True)
        summary = {"tasks": 6, "training_admissions": 0}
        comparison = ai.compare(self.root, self.raw, initial, summary, summary)
        ai.activate(self.root, initial, comparison)
        self.before = self.collect()
        self.summary = summary
        self.audit_path.write_text(json.dumps(self.before))

    def collect(self, *, all_members=False):
        with collect_inputs(self.root, archive_ref=self.ref) as state:
            for name in self.names if all_members else self.names[:1]: read_json(self.root/name, self.root)
            track_input(self.root/self.source, self.root)
            policy = ai.read_policy(self.root)
            if policy is not None:
                read_json(self.root/ai.POLICY, self.root)
                read_json(self.root/policy["comparison_ref"], self.root)
        return {**self.raw, "input_sha256": state["files"],
            "checks": [*self.raw["checks"], {"name": ai.CHECK, "passed": True}],
            **ai.fields(self.root, state["files"], state["archive"], state["archived"])}

    def candidate(self):
        result = ea.create(self.root, rebase=True)
        self.ref = result["archive_ref"]
        self.after = self.collect()
        return result

    def compare(self):
        return ai.compare_archives(self.root, self.before, self.after, self.summary, self.summary)

    def rewrite(self, transform):
        original = self.root/self.ref
        target = original.with_name("unit-mutant.zip")
        with zipfile.ZipFile(original) as src, zipfile.ZipFile(target, "w") as dst:
            for info in src.infolist():
                raw = src.read(info)
                info, raw = transform(info, raw)
                dst.writestr(info, raw)
        destination = target.with_name(hashlib.sha256(target.read_bytes()).hexdigest()+".zip")
        os.link(target, destination)
        target.unlink()
        return destination.relative_to(self.root).as_posix()

    def test_inspection_is_read_only_and_counts_metadata_cost(self):
        before = {p:p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = ea.inspect(self.root)
        self.assertEqual(result["unused_previous_members"], 1)
        self.assertEqual(result["eligible_members"], 1)
        self.assertFalse(result["creates_archive"])
        self.assertFalse(result["policy_changed"])
        self.assertEqual(before, {p:p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_v2_selects_actual_members_and_preserves_prior_archive_and_originals(self):
        old = (self.root/self.previous).read_bytes()
        result = self.candidate()
        self.assertEqual(result["schema_version"], ea.REBASED_SCHEMA)
        self.assertEqual(result["member_count"], 1)
        self.assertEqual((self.root/self.previous).read_bytes(), old)
        self.assertTrue((self.root/self.names[1]).is_file())
        with zipfile.ZipFile(self.root/self.ref) as archive:
            self.assertIn(ea.SELECTION, archive.namelist())
            self.assertNotIn(self.previous, archive.namelist())
            self.assertNotIn(self.names[1], archive.namelist())
        self.assertTrue(inputs_current(self.root, self.after))

    def test_v1_create_still_rejects_grouped_audit(self):
        with self.assertRaisesRegex(ValueError, "current passing"): ea.create(self.root)

    def test_v2_verification_has_no_recursive_previous_archive_dependency(self):
        self.candidate()
        previous = self.root/self.previous
        previous.rename(previous.with_suffix(".fixture-history"))
        self.assertTrue(ea.verify(self.root, self.ref)["passed"])
        self.assertTrue(inputs_current(self.root, self.after))

    def test_same_inputs_reuse_v2_immutable_archive(self):
        first = self.candidate()
        second = ea.create(self.root, rebase=True)
        self.assertEqual(second["archive_ref"], first["archive_ref"])
        self.assertTrue(second["reused"])

    def test_metadata_file_sizes_are_all_charged(self):
        self.candidate()
        loaded = ai.load(self.root, self.ref)
        with zipfile.ZipFile(self.root/self.ref) as archive:
            total = sum(archive.getinfo(n).file_size for n in (ea.MANIFEST, ea.AUDIT_COPY, ea.SELECTION))
        self.assertEqual(loaded["metadata_bytes"], total)
        direct = sum((self.root/n).stat().st_size for n in self.after["input_sha256"] if n != self.ref)
        self.assertEqual(self.after["input_budget"]["logical_bytes"], direct+loaded["observed"]["expanded_bytes"]+total)

    def test_missing_or_changed_original_still_invalidates_v2(self):
        self.candidate()
        original = self.root/self.names[0]
        original.write_bytes(b"changed")
        self.assertFalse(inputs_current(self.root, self.after))
        original.unlink()
        self.assertFalse(inputs_current(self.root, self.after))

    def test_selection_hash_and_inventory_changes_are_rejected(self):
        self.candidate()
        def change(info, raw):
            if info.filename == ea.SELECTION:
                value = json.loads(raw)
                value["input_sha256"][self.names[0]] = "0"*64
                raw = json.dumps(value).encode()
            return info, raw
        ref = self.rewrite(change)
        with self.assertRaisesRegex(ValueError, "source audit digest"): ea.verify(self.root, ref)

    def test_selection_unknown_schema_and_metadata_fields_are_rejected(self):
        self.candidate()
        for value in ({"schema_version": "flat", "input_sha256": {}},
                      {"schema_version": "archive-logical-selection-v1", "input_sha256": {}, "ignored": True}):
            ref = self.rewrite(lambda info, raw: (info, json.dumps(value).encode() if info.filename == ea.SELECTION else raw))
            with self.assertRaises(ValueError): ea.verify(self.root, ref)

    def test_grouped_logical_selection_can_exceed_flat_4096_limit(self):
        audit = deepcopy(self.before)
        ref = audit["archive_inputs"]["archive_ref"]
        direct = {"integrations/fixture"+str(i)+".py": "a"*64 for i in range(4095)}
        used = ["runs/unit-proof/archived.json", "runs/unit-proof/current.json"]
        logical = {**direct, **{n:"b"*64 for n in used}}
        audit["input_sha256"] = {ref:"c"*64, **direct}
        audit["archive_inputs"]["used_members"] = used
        audit["logical_input_manifest_sha256"] = ai.digest(logical)
        selected = ea._selection(audit, {"schema_version":"archive-logical-selection-v1","input_sha256":logical})
        self.assertEqual(len(selected), 4097)

    def test_selection_bounds_paths_and_direct_overlaps_are_rejected(self):
        authority = ai.load(self.root, self.previous)
        logical = ai.logical_inputs(self.before["input_sha256"], authority, self.before["archive_inputs"]["used_members"])
        for name in ("../escape", "runs//unit/x.json", "C:/private", "runs/unit/a\\b.json"):
            changed = {**logical, name:"a"*64}
            with self.assertRaises(ValueError): ea._selection(self.before, {"schema_version":"archive-logical-selection-v1","input_sha256":changed})
        with self.assertRaises(ValueError):
            ea._selection(self.before, {"schema_version":"archive-logical-selection-v1","input_sha256":{str(i):"a"*64 for i in range(8192)}})
        audit = deepcopy(self.before)
        audit["input_sha256"][self.names[0]] = logical[self.names[0]]
        with self.assertRaises(ValueError): ea._selection(audit, {"schema_version":"archive-logical-selection-v1","input_sha256":logical})

    def test_candidate_budget_is_checked_before_publishing(self):
        old = ai.policy_sha256(self.root)
        prepared = ea._source(self.root, True)
        with patch.object(ea, "_source", return_value=prepared), patch.object(ea, "MAX_CONTENT", self.before["input_budget"]["logical_bytes"]):
            with self.assertRaisesRegex(ValueError, "128 MiB"): ea.create(self.root, rebase=True)
        self.assertEqual(ai.policy_sha256(self.root), old)

    def test_stale_audit_and_nonboolean_mode_are_rejected(self):
        (self.root/self.source).write_text("# changed")
        with self.assertRaisesRegex(ValueError, "current passing"): ea.create(self.root, rebase=True)
        with self.assertRaises(ValueError): ea.create(self.root, rebase=1)

    def test_grouped_comparison_preserves_checks_inputs_and_qualification(self):
        self.candidate()
        comparison = self.compare()
        self.assertEqual(comparison["matched_existing_checks"], 1)
        self.assertEqual(comparison["matched_logical_inputs"], len(self.before["archive_inputs"]["used_members"])+len(self.before["input_sha256"])-1)
        self.assertEqual(comparison["previous_archive_ref"], self.previous)
        self.assertFalse(comparison["originals_removed"])

    def test_changed_check_context_or_qualification_blocks_comparison(self):
        self.candidate()
        for key, value in (("checks", [{"name":"other","passed":True},{"name":ai.CHECK,"passed":True}]),
                           ("contracts_sha256", "a"*64)):
            after = deepcopy(self.after)
            after[key] = value
            with self.assertRaises(ValueError): ai.compare_archives(self.root, self.before, after, self.summary, self.summary)
        with self.assertRaises(ValueError): ai.compare_archives(self.root, self.before, self.after, self.summary, {"tasks":7})

    def test_rebase_activation_requires_exact_policy_digest(self):
        self.candidate()
        comparison = self.compare()
        old = (self.root/ai.POLICY).read_bytes()
        for sha in (None, "0"*64, True):
            with self.assertRaises(ValueError): ai.activate(self.root, self.after, comparison, expected_policy_sha256=sha)
            self.assertEqual(old, (self.root/ai.POLICY).read_bytes())

    def test_atomic_rebase_activation_retains_policy_history_and_invalidates_old_receipts(self):
        self.candidate()
        comparison = self.compare()
        old = ai.read_policy(self.root)
        selected = ai.activate(self.root, self.after, comparison, expected_policy_sha256=ai.policy_sha256(self.root))
        self.assertEqual(selected["archive_ref"], self.ref)
        self.assertEqual(ai.read_policy(self.root), selected)
        self.assertFalse(inputs_current(self.root, self.before))
        self.assertFalse(inputs_current(self.root, self.after))
        self.assertTrue(inputs_current(self.root, self.collect()))
        self.assertTrue(list((self.root/ea.DIRECTORY/"policy-history").glob("*.json")))
        self.assertNotEqual(selected, old)

    def test_forged_comparison_and_changed_historical_snapshot_block_activation(self):
        self.candidate()
        comparison = self.compare()
        forged = {**comparison, "matched_logical_inputs": 0}
        sha = ai.policy_sha256(self.root)
        with self.assertRaisesRegex(ValueError, "recomputed"): ai.activate(self.root, self.after, forged, expected_policy_sha256=sha)
        (self.root/comparison["before_audit_ref"]).write_text("{}")
        with self.assertRaisesRegex(ValueError, "snapshot changed"): ai.activate(self.root, self.after, comparison, expected_policy_sha256=sha)
        self.assertEqual(ai.policy_sha256(self.root), sha)

    def test_existing_activation_lock_blocks_another_writer(self):
        self.candidate()
        comparison = self.compare()
        marker = self.root/ea.DIRECTORY/"activate.lock"
        marker.write_text("explicit unit owner")
        with self.assertRaises(FileExistsError):
            ai.activate(self.root, self.after, comparison, expected_policy_sha256=ai.policy_sha256(self.root))
        self.assertEqual(marker.read_text(), "explicit unit owner")

    def test_atomic_policy_replace_failure_preserves_old_bytes(self):
        self.candidate()
        comparison = self.compare()
        old = (self.root/ai.POLICY).read_bytes()
        with patch("workenv.core.replace_atomic", side_effect=OSError("unit replace interrupted")):
            with self.assertRaises(OSError): ai.activate(self.root, self.after, comparison, expected_policy_sha256=ai.policy_sha256(self.root))
        self.assertEqual((self.root/ai.POLICY).read_bytes(), old)
        self.assertFalse((self.root/ea.DIRECTORY/"activate.lock").exists())

    def test_interrupted_candidate_publish_retains_pending_and_old_authority(self):
        old = ai.policy_sha256(self.root)
        with patch.object(ea.os, "link", side_effect=OSError("unit interrupted publication")):
            with self.assertRaises(OSError): ea.create(self.root, rebase=True)
        self.assertEqual(ai.policy_sha256(self.root), old)
        pending = list((self.root/ea.DIRECTORY).glob("pending-*.zip"))
        self.assertEqual(len(pending), 1)
        self.assertTrue(ea._verify(self.root, pending[0])["passed"])

    def test_changed_policy_between_validation_and_publish_is_not_overwritten(self):
        self.candidate()
        comparison = self.compare()
        expected = ai.policy_sha256(self.root)
        original = ai.policy_sha256
        count = 0
        def raced(root):
            nonlocal count
            count += 1
            if count == 2:
                path = self.root/ai.POLICY
                path.write_bytes(path.read_bytes()+b"\n")
            return original(root)
        with patch.object(ai, "policy_sha256", side_effect=raced):
            with self.assertRaisesRegex(ValueError, "before publication"):
                ai.activate(self.root, self.after, comparison, expected_policy_sha256=expected)
        self.assertNotEqual(ai.policy_sha256(self.root), expected)
        self.assertEqual(ai.read_policy(self.root)["archive_ref"], self.previous)


if __name__ == "__main__": unittest.main()
