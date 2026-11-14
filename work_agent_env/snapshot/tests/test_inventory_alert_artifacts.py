"""Real temporary-file fault injection; all cluster identities are fixtures."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adapters import inventory_alert_artifacts as artifacts
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile
from workenv.core import ResourceError, save


class InventoryArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = EpisodeProfile(self.root, "f1895a7d-8626-4818-839a-d5ddab57b4a0", 0)
        self.workspace = self.profile.base / "inventory-workspace"; self.workspace.mkdir(parents=True)
        save(self.profile.base / "cluster_owner.json", self.profile.record())
        self.marker = self.profile.base / "inventory-initialized.json"
        self.initialized = {"state": "ready", "profile": self.profile.record(),
            "task_id": artifacts.TASK_ID, "contract_source": artifacts.source_identity()}
        save(self.marker, self.initialized)
        self.cluster = EpisodeCluster(self.profile); self.cluster.started = True
        self.target = self.workspace / artifacts.FILENAME

    def intents(self):
        return list(self.profile.base.glob(artifacts.PREFIX + "*.json"))

    def pending(self, *, before=b"old", after=b"new"):
        if before is not None:
            artifacts.write_report(self.root, self.cluster, before.decode())
        with patch.object(artifacts, "replace_atomic", side_effect=PermissionError("injected before replace")):
            with self.assertRaises(PermissionError): artifacts.write_report(self.root, self.cluster, after.decode())
        return next(path for path in self.intents() if json.loads(path.read_text())["state"] == "prepared")

    def test_utf8_roundtrip_fixed_name_and_exact_previous_bytes_preserved(self):
        text = "7 - \u8336 - SKU7 - Food\r\n"
        first = artifacts.write_report(self.root, self.cluster, text)
        read = artifacts.read_report(self.root, self.cluster)
        self.assertEqual(read["text"], text)
        self.assertEqual(read["bytes"], len(text.encode()))
        self.assertEqual(read["sha256"], first["sha256"])
        self.assertTrue(read["artifact_ref"].endswith("/out_of_stock_report.txt"))
        second = artifacts.write_report(self.root, self.cluster, "measurably wrong business content")
        self.assertFalse(second["task_content_evaluated"])
        self.assertEqual(second["previous_sha256"], first["sha256"])
        history = self.profile.base / "inventory-artifact-history" / (first["sha256"] + ".txt")
        self.assertEqual(history.read_bytes(), text.encode())

    def test_empty_report_is_valid_and_bytes_not_character_count_bound_output(self):
        self.assertEqual(artifacts.write_report(self.root, self.cluster, "")["bytes"], 0)
        self.assertEqual(artifacts.read_report(self.root, self.cluster)["text"], "")
        for text in ("\u8336" * (artifacts.MAX_TEXT_BYTES // 3 + 1), "\ud800", "bad\0text", None, b"text"):
            with self.assertRaises(ValueError): artifacts.write_report(self.root, self.cluster, text)
        self.assertEqual(self.target.read_bytes(), b"")

    def test_foreign_closed_wrong_task_source_and_unready_markers_reject(self):
        with self.assertRaises(ValueError): artifacts.write_report(self.root / "foreign", self.cluster, "x")
        self.cluster.started = False
        with self.assertRaises(ResourceError): artifacts.read_report(self.root, self.cluster)
        self.cluster.started = True
        for key, value in (("task_id", "wc-customer-order-gsheet-email"), ("profile", {}),
                           ("contract_source", {}), ("state", "initializing")):
            save(self.marker, {**self.initialized, key: value})
            with self.assertRaises(ResourceError): artifacts.write_report(self.root, self.cluster, "x")
        self.assertFalse(self.target.exists())

    def test_independent_workspaces_do_not_share_files_or_history(self):
        other = EpisodeProfile(self.root, "6edffaad-25a8-4f8c-8990-fdab0b11dff0", 1)
        directory = other.base / "inventory-workspace"; directory.mkdir(parents=True)
        save(other.base / "cluster_owner.json", other.record())
        save(other.base / "inventory-initialized.json", {**self.initialized, "profile": other.record()})
        second = EpisodeCluster(other); second.started = True
        artifacts.write_report(self.root, self.cluster, "first")
        artifacts.write_report(self.root, second, "second")
        artifacts.write_report(self.root, self.cluster, "first updated")
        self.assertEqual(artifacts.read_report(self.root, second)["text"], "second")
        self.assertFalse((other.base / "inventory-artifact-history").exists())

    def test_before_replace_failure_blocks_reads_and_writes_until_noncommit_reconciled(self):
        pending = self.pending(); original = pending.read_bytes()
        self.assertEqual(self.target.read_bytes(), b"old")
        for operation in (lambda: artifacts.read_report(self.root, self.cluster),
                          lambda: artifacts.write_report(self.root, self.cluster, "retry")):
            with self.assertRaises(ResourceError): operation()
        with patch.object(artifacts, "replace_atomic") as replace:
            result = artifacts.reconcile_write(self.root, self.cluster, pending.name)
        replace.assert_not_called()
        self.assertEqual(result["decision"], "not_committed")
        self.assertEqual((self.root / result["original_intent_ref"]).read_bytes(), original)
        self.assertEqual(artifacts.read_report(self.root, self.cluster)["text"], "old")
        self.assertFalse(list(self.workspace.glob(".inventory-text-*.tmp")))

    def test_after_replace_metadata_failure_reconciles_commit_without_rewrite(self):
        real_save = artifacts.save
        def interrupted(path, value):
            if value.get("state") == "completed": raise OSError("injected after file commit")
            return real_save(path, value)
        with patch.object(artifacts, "save", side_effect=interrupted):
            with self.assertRaises(OSError): artifacts.write_report(self.root, self.cluster, "committed")
        pending = self.intents()[0]; original = pending.read_bytes()
        self.cluster.started = False
        with patch.object(artifacts, "replace_atomic") as replace:
            result = artifacts.reconcile_write(self.root, self.cluster, pending.name)
        replace.assert_not_called()
        self.assertEqual(result["decision"], "committed")
        self.assertFalse(result["replay_performed"])
        self.assertEqual(self.target.read_bytes(), b"committed")
        self.assertEqual((self.root / result["original_intent_ref"]).read_bytes(), original)
        resolved = pending.read_bytes()
        self.assertEqual(artifacts.reconcile_write(self.root, self.cluster, pending.name)["decision"], "already_resolved")
        self.assertEqual(pending.read_bytes(), resolved)

    def test_missing_old_file_and_empty_committed_file_are_distinct(self):
        pending = self.pending(before=None, after=b"")
        self.assertFalse(self.target.exists())
        result = artifacts.reconcile_write(self.root, self.cluster, pending.name)
        self.assertEqual(result["decision"], "not_committed")
        pending = self.pending(before=None, after=b"")
        self.target.write_bytes(b"")
        result = artifacts.reconcile_write(self.root, self.cluster, pending.name)
        self.assertEqual(result["decision"], "committed")

    def test_unknown_bytes_keep_artifact_and_intent_unchanged(self):
        pending = self.pending(); original = pending.read_bytes()
        self.target.write_bytes(b"unrecognized")
        with self.assertRaises(ResourceError): artifacts.reconcile_write(self.root, self.cluster, pending.name)
        self.assertEqual(pending.read_bytes(), original)
        self.assertEqual(self.target.read_bytes(), b"unrecognized")

    def test_forged_intent_fields_and_path_traversal_are_refused(self):
        pending = self.pending(); original = json.loads(pending.read_text())
        for key, value in (("profile", {}), ("task_id", "other-task"), ("contract_source", {}),
            ("artifact_ref", "elsewhere.txt"), ("bytes", -1), ("bytes", True), ("sha256", "wrong"),
            ("previous_sha256", "wrong"), ("state", "other"), ("task_content_evaluated", True)):
            save(pending, {**original, key: value})
            with self.assertRaises(ResourceError): artifacts.reconcile_write(self.root, self.cluster, pending.name)
        save(pending, original)
        for name in ("../" + pending.name, str(pending), "inventory-artifact-write-fixture.json"):
            with self.assertRaises(ValueError): artifacts.reconcile_write(self.root, self.cluster, name)
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_existing_lock_is_preserved_and_never_automatically_recovered(self):
        pending = self.pending(); lock = self.profile.base / ".inventory-artifact.lock"
        lock.write_bytes(b"unknown or live writer")
        for operation in (lambda: artifacts.write_report(self.root, self.cluster, "x"),
                          lambda: artifacts.reconcile_write(self.root, self.cluster, pending.name)):
            with self.assertRaises(FileExistsError): operation()
            self.assertEqual(lock.read_bytes(), b"unknown or live writer")

    def test_external_change_after_prepare_is_not_overwritten_or_silently_retried(self):
        artifacts.write_report(self.root, self.cluster, "old")
        real_save = artifacts.save
        def changed(path, value):
            real_save(path, value)
            if value.get("state") == "prepared": self.target.write_bytes(b"external writer")
        with patch.object(artifacts, "save", side_effect=changed), patch.object(artifacts, "replace_atomic") as replace:
            with self.assertRaises(ResourceError): artifacts.write_report(self.root, self.cluster, "new")
        replace.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b"external writer")
        pending = next(path for path in self.intents() if json.loads(path.read_text())["state"] == "prepared")
        with self.assertRaises(ResourceError): artifacts.reconcile_write(self.root, self.cluster, pending.name)

    def test_malformed_initialization_and_intent_keep_state_unknown(self):
        for raw in (b'[]', b'{', b'\xff'):
            self.marker.write_bytes(raw)
            with self.assertRaises(ResourceError): artifacts.write_report(self.root, self.cluster, "x")
        save(self.marker, self.initialized)
        pending = self.pending()
        for raw in (b'[]', b'{', b'\xff'):
            pending.write_bytes(raw)
            with self.assertRaises(ResourceError): artifacts.reconcile_write(self.root, self.cluster, pending.name)
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_history_budget_blocks_new_mutation_without_deleting_old_records(self):
        with patch.object(artifacts, "MAX_WRITES", 2):
            artifacts.write_report(self.root, self.cluster, "one")
            artifacts.write_report(self.root, self.cluster, "two")
            old = {path.name: path.read_bytes() for path in self.intents()}
            with self.assertRaises(ResourceError): artifacts.write_report(self.root, self.cluster, "three")
            self.assertEqual({path.name: path.read_bytes() for path in self.intents()}, old)
            self.assertEqual(self.target.read_bytes(), b"two")

    def test_corrupt_history_does_not_get_overwritten_by_new_write(self):
        first = artifacts.write_report(self.root, self.cluster, "first")
        history = self.profile.base / "inventory-artifact-history" / (first["sha256"] + ".txt")
        history.parent.mkdir(); history.write_bytes(b"corrupted archive")
        with self.assertRaises(ResourceError): artifacts.write_report(self.root, self.cluster, "second")
        self.assertEqual(self.target.read_bytes(), b"first")
        self.assertEqual(history.read_bytes(), b"corrupted archive")

    def test_linked_target_is_refused_without_touching_external_bytes(self):
        outside = self.root / "outside.txt"; outside.write_bytes(b"external")
        try: self.target.symlink_to(outside)
        except OSError: self.skipTest("Creating symbolic links is unavailable")
        for operation in (lambda: artifacts.write_report(self.root, self.cluster, "new"),
                          lambda: artifacts.read_report(self.root, self.cluster)):
            with self.assertRaises(ValueError): operation()
        self.assertEqual(outside.read_bytes(), b"external")

    def test_externally_invalid_utf8_is_unknown_and_missing_file_is_not_empty_report(self):
        with self.assertRaises(ResourceError): artifacts.read_report(self.root, self.cluster)
        self.target.write_bytes(b"\xff")
        with self.assertRaises(ResourceError): artifacts.read_report(self.root, self.cluster)
        self.assertEqual(self.target.read_bytes(), b"\xff")


if __name__ == "__main__": unittest.main()
