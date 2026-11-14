"""Evidence retention and adversarial archive boundaries, without services."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from integrations import evidence_archive as ea
from integrations.mainline_audit import inputs_current


class EvidenceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.name = ".runtime/native-episodes/unit-proof/input.json"
        self.original = self.root / self.name
        self.original.parent.mkdir(parents=True)
        self.original.write_bytes(b'{"fixture":1}\n')
        self.source = self.root / "integrations/unit.py"
        self.source.parent.mkdir()
        self.source.write_bytes(b"UNIT = True\n")
        self.audit = {"schema_version": "mainline-consistency-audit-v1", "passed": True,
                      "checks": [{"name": "unit-only", "passed": True}], "input_sha256": {}}
        self.audit_path = self.root / ea.AUDIT
        self.audit_path.parent.mkdir(parents=True)
        self.write_audit()

    def write_audit(self):
        self.audit["input_sha256"] = {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in (self.original, self.source)}
        self.audit_path.write_text(json.dumps(self.audit), encoding="utf-8")

    def create(self):
        result = ea.create(self.root)
        self.path = self.root / result["archive_ref"]
        return result

    def rewritten(self, manifest=None, extra=(), transform=None):
        """Generate a content-addressed adversarial ZIP, never edit the original."""
        with zipfile.ZipFile(self.path) as archive:
            rows = [(info, archive.read(info)) for info in archive.infolist()]
        target = self.path.parent / "test-building.zip"
        with warnings.catch_warnings(), zipfile.ZipFile(target, "w") as archive:
            warnings.simplefilter("ignore", UserWarning)
            for info, raw in rows:
                if info.filename == ea.MANIFEST and manifest is not None:
                    raw = manifest if isinstance(manifest, bytes) else json.dumps(manifest).encode()
                if transform: info, raw = transform(info, raw)
                archive.writestr(info, raw)
            for info, raw in extra: archive.writestr(info, raw)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        destination = target.with_name(digest + ".zip")
        target.rename(destination)
        return destination.relative_to(self.root).as_posix()

    def manifest(self):
        with zipfile.ZipFile(self.path) as archive: return json.loads(archive.read(ea.MANIFEST))

    def test_create_verifies_originals_without_releasing_audit_capacity(self):
        before = self.original.read_bytes(), self.audit_path.read_bytes()
        result = self.create()
        verified = ea.verify(self.root, result["archive_ref"])
        self.assertEqual(verified["member_count"], 1)
        self.assertEqual(verified["expanded_bytes"], len(before[0]))
        self.assertEqual(result["mainline_input_capacity_released"], 0)
        self.assertFalse(result["mainline_collector_integrated"])
        self.assertEqual(before, (self.original.read_bytes(), self.audit_path.read_bytes()))
        self.assertTrue(inputs_current(self.root, self.audit))
        with zipfile.ZipFile(self.path) as archive: self.assertNotIn("integrations/unit.py", archive.namelist())

    def test_identical_inputs_reuse_immutable_archive(self):
        result = self.create()
        stamp = self.path.stat().st_mtime_ns
        again = ea.create(self.root)
        self.assertEqual(result["archive_ref"], again["archive_ref"])
        self.assertTrue(again["reused"])
        self.assertEqual(stamp, self.path.stat().st_mtime_ns)
        self.assertFalse(list(self.path.parent.glob("pending-*.zip")))

    def test_same_length_original_mutation_invalidates_copy_and_current_audit(self):
        result = self.create()
        self.original.write_bytes(b'{"fixture":2}\n')
        with self.assertRaisesRegex(ValueError, "original bytes changed"): ea.verify(self.root, result["archive_ref"])
        self.assertFalse(inputs_current(self.root, self.audit))

    def test_missing_original_is_not_replaced_by_archive(self):
        result = self.create()
        self.original.unlink()
        with self.assertRaisesRegex(ValueError, "retained original"): ea.verify(self.root, result["archive_ref"])

    def test_stale_or_failed_audit_refuses_creation_before_writing(self):
        for mode in ("stale", "failed", "duplicate"):
            with self.subTest(mode=mode):
                self.write_audit()
                value = json.loads(self.audit_path.read_text())
                if mode == "stale": value["input_sha256"][self.name] = "0" * 64
                if mode == "failed": value["checks"][0]["passed"] = False
                if mode == "duplicate": value["checks"] *= 2
                self.audit_path.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, "current passing"): ea.create(self.root)
                self.assertFalse((self.root / ea.DIRECTORY).exists())

    def test_path_allowlist_rejects_source_personal_upstream_and_pg_files(self):
        for name in ("README.md", "weekly_report.md", "integrations/code.py", ".upstream/task/evaluator.py",
                     "evidence/mainline/summary.json", ".runtime/native-episodes/unit/pgdata/config.json",
                     ".runtime/native-episodes/unit/data/payload.json", ".runtime/native-episodes/unit/password.txt",
                     "runs/unit/.env", "runs/unit/../another/x.json", "runs//unit/x.json",
                     "runs/unit/a\\b.json", "runs/unit/a:stream.json", "runs/unit/a. /x.json"):
            with self.subTest(name=name), self.assertRaises(ValueError): ea.evidence_name(name)

    def test_original_symlink_is_rejected(self):
        target = self.original.with_name("linked-target.json")
        target.write_bytes(self.original.read_bytes())
        self.original.unlink()
        try: self.original.symlink_to(target)
        except OSError:
            self.skipTest("Host does not allow symbolic link creation; Linux runs this guard")
        with self.assertRaises(ValueError): ea.create(self.root)

    def test_zip_symlink_is_rejected(self):
        self.create()
        def transform(info, raw):
            if info.filename == self.name: info.external_attr = (stat.S_IFLNK | 0o777) << 16
            return info, raw
        ref = self.rewritten(transform=transform)
        with self.assertRaisesRegex(ValueError, "member type"): ea.verify(self.root, ref)

    def test_extra_and_duplicate_members_are_rejected(self):
        self.create()
        for extra in (((self.name, b"duplicate"),), (("runs/unit/unlisted.json", b"{}"),)):
            with self.subTest(extra=extra):
                ref = self.rewritten(extra=extra)
                with self.assertRaises(ValueError): ea.verify(self.root, ref)

    def test_traversal_member_is_rejected_without_extracting(self):
        self.create()
        ref = self.rewritten(extra=(("../outside.json", b"{}"),))
        with self.assertRaises(ValueError): ea.verify(self.root, ref)
        self.assertFalse((self.root.parent / "outside.json").exists())

    def test_member_hash_and_size_are_independently_recomputed(self):
        self.create()
        for key, replacement in (("sha256", "0" * 64), ("size", 0), ("size", True)):
            value = self.manifest()
            value["members"][self.name][key] = replacement
            ref = self.rewritten(manifest=value)
            with self.subTest(key=key, replacement=replacement), self.assertRaises(ValueError): ea.verify(self.root, ref)

    def test_corrupted_member_rejected_even_with_new_archive_sha(self):
        self.create()
        def transform(info, raw): return info, raw.replace(b'"fixture":1', b'"fixture":2')
        ref = self.rewritten(transform=transform)
        with self.assertRaisesRegex(ValueError, "member bytes changed"): ea.verify(self.root, ref)

    def test_captured_audit_is_retained_and_verified_after_live_report_changes(self):
        result = self.create()
        captured = self.audit_path.read_bytes()
        with zipfile.ZipFile(self.path) as archive: self.assertEqual(archive.read(ea.AUDIT_COPY), captured)
        def transform(info, raw):
            if info.filename == ea.AUDIT_COPY: raw = raw.replace(b'"passed": true', b'"passed":false', 1)
            return info, raw
        ref = self.rewritten(transform=transform)
        with self.assertRaisesRegex(ValueError, "source audit bytes changed"): ea.verify(self.root, ref)
        self.audit_path.write_text("{}")
        self.assertTrue(ea.verify(self.root, result["archive_ref"])["passed"])

    def test_duplicate_manifest_keys_are_rejected(self):
        self.create()
        ref = self.rewritten(manifest=b'{"schema_version":"a","schema_version":"b"}')
        with self.assertRaisesRegex(ValueError, "Duplicate JSON"): ea.verify(self.root, ref)

    def test_compressed_and_expanded_budgets_are_independent(self):
        result = self.create()
        with patch.object(ea, "MAX_ARCHIVE", 1), self.assertRaisesRegex(ValueError, "compressed"):
            ea.verify(self.root, result["archive_ref"])
        with patch.object(ea, "MAX_CONTENT", 1), self.assertRaisesRegex(ValueError, "Expanded"):
            ea.verify(self.root, result["archive_ref"])
        with patch.object(ea, "MAX_FILE", 1), self.assertRaisesRegex(ValueError, "member exceeds"):
            ea.verify(self.root, result["archive_ref"])

    def test_manifest_and_member_count_budgets_are_enforced(self):
        result = self.create()
        with patch.object(ea, "MAX_MANIFEST", 1), self.assertRaisesRegex(ValueError, "member exceeds"):
            ea.verify(self.root, result["archive_ref"])
        with patch.object(ea, "MAX_MEMBERS", 0), self.assertRaisesRegex(ValueError, "entry count"):
            ea.verify(self.root, result["archive_ref"])

    def test_wrong_archive_name_and_nonprivate_reference_are_rejected(self):
        self.create()
        other = self.path.with_name("0" * 64 + ".zip")
        other.write_bytes(self.path.read_bytes())
        with self.assertRaisesRegex(ValueError, "filename mismatch"):
            ea.verify(self.root, other.relative_to(self.root).as_posix())
        with self.assertRaises(ValueError): ea.verify(self.root, "evidence/public.zip")
        with self.assertRaises(ValueError): ea.verify(self.root, "Xruntime/evidence-archives/" + "0" * 64 + ".zip")

    def test_old_source_audit_digest_is_provenance_not_claim_of_current_acceptance(self):
        result = self.create()
        self.audit_path.write_text("{}")
        verified = ea.verify(self.root, result["archive_ref"])
        self.assertTrue(verified["passed"])
        self.assertEqual(verified["source_audit"], result["source_audit"])
        with self.assertRaisesRegex(ValueError, "current passing"): ea.create(self.root)

    def test_interrupted_publication_retains_pending_and_all_original_bytes(self):
        original = self.original.read_bytes()
        with patch.object(ea.os, "link", side_effect=OSError("injected publication fault")):
            with self.assertRaisesRegex(OSError, "publication fault"): ea.create(self.root)
        pending = list((self.root / ea.DIRECTORY).glob("pending-*.zip"))
        self.assertEqual(len(pending), 1)
        self.assertTrue(ea._verify(self.root, pending[0])["passed"])
        self.assertEqual(self.original.read_bytes(), original)
        self.assertFalse((pending[0].parent / "create.lock").exists())

    @unittest.skipIf(os.name == "nt", "POSIX permissions are checked in Linux")
    def test_new_private_archive_has_owner_only_permissions(self):
        self.create()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)


if __name__ == "__main__": unittest.main()
