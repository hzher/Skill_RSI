import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from integrations import ledger_rotation as rotation
from integrations.resource_ledger import ResourceLedger


class LedgerRotationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ledger-generation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / ".runtime/nemo-vm/test-sessions.json"
        self.ledger = ResourceLedger(self.path, self.root)
        self.sid = str(uuid.uuid4())
        self.binding = {"episode_id": {"rollout_id": "unit-closed"}}
        self.ledger.seed(self.sid, self.binding)
        self.ledger.update(self.sid, state="closed")

    def rotate(self):
        return rotation.rotate_ledger(self.path, self.root, expected_sha256=rotation.rotation_plan(self.path, self.root)["expected_sha256"])

    def test_dry_run_only_observes_metadata(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        plan = rotation.rotation_plan(self.path, self.root)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["closing_identities_retained"], 1)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_retired_identity_never_reopens_but_close_remains_idempotent(self):
        old = self.path.read_bytes()
        result = self.rotate()
        self.assertEqual(old, (self.root / result["archive_ref"]).read_bytes())
        fresh = ResourceLedger(self.path, self.root)
        self.assertEqual(fresh.rows, {})
        self.assertIn(self.sid, fresh.retired)
        with self.assertRaises(ValueError): fresh.seed(self.sid, self.binding)
        fresh.close_unknown(self.sid, self.binding)
        with self.assertRaises(ValueError): fresh.close_unknown(self.sid, {"episode_id": {"rollout_id": "another"}})
        fresh.seed(str(uuid.uuid4()), {"episode_id": {"rollout_id": "new-generation"}})
        self.assertEqual(len(fresh.rows), 1)

    def test_active_generation_stale_digest_or_live_bridge_refuses_rotation(self):
        with self.assertRaises(ValueError): rotation.rotate_ledger(self.path, self.root, expected_sha256="a" * 64)
        sid = str(uuid.uuid4())
        self.ledger.seed(sid, self.binding)
        self.ledger.update(sid, state="active")
        with self.assertRaises(ValueError): rotation.rotation_plan(self.path, self.root)
        self.ledger.update(sid, state="closed")
        lock = self.root / ".runtime/native_task.lock"
        lock.write_text("unit-live-resource", encoding="ascii")
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeError): self.rotate()
        self.assertEqual(before, self.path.read_bytes())

    def test_partial_publish_blocks_startup_and_explicit_recovery_rolls_back(self):
        before = self.path.read_bytes()
        original = rotation.publish
        def fail_current(path, raw, owner):
            if path == self.path: raise OSError("unit interrupted second publish")
            return original(path, raw, owner)
        with patch.object(rotation, "publish", side_effect=fail_current):
            with self.assertRaises(OSError): self.rotate()
        with self.assertRaisesRegex(ValueError, "Unfinished"): ResourceLedger(self.path, self.root)
        result = rotation.recover_rotation(self.path, self.root)
        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(ResourceLedger(self.path, self.root).retired, {})

    def test_completed_bytes_can_finish_an_interrupted_commit_marker(self):
        original = rotation.publish
        def fail_complete(path, raw, owner):
            if path.name == "rotation-pending.json" and json.loads(raw).get("state") == "completed":
                raise OSError("unit interrupted commit marker")
            return original(path, raw, owner)
        with patch.object(rotation, "publish", side_effect=fail_complete):
            with self.assertRaises(OSError): self.rotate()
        self.assertEqual(rotation.recover_rotation(self.path, self.root)["state"], "completed")
        self.assertIn(self.sid, ResourceLedger(self.path, self.root).retired)

    def test_tampered_archive_or_unknown_transaction_bytes_are_preserved(self):
        result = self.rotate()
        archive = self.root / result["archive_ref"]
        archive.write_bytes(b"changed unit archive")
        with self.assertRaises(ValueError): ResourceLedger(self.path, self.root)

    def test_identity_lookup_spans_multiple_generations(self):
        self.rotate()
        fresh = ResourceLedger(self.path, self.root)
        second = str(uuid.uuid4())
        fresh.seed(second, {"episode_id": {"rollout_id": "unit-two"}})
        fresh.update(second, state="closed")
        self.rotate()
        latest = ResourceLedger(self.path, self.root)
        self.assertEqual(set(latest.retired), {self.sid, second})
        self.assertEqual(latest.rows, {})

    def test_history_budget_is_checked_before_any_commit(self):
        first_size = self.path.stat().st_size
        self.rotate()
        fresh = ResourceLedger(self.path, self.root)
        second = str(uuid.uuid4())
        fresh.seed(second, self.binding)
        fresh.update(second, state="closed")
        before = self.path.read_bytes()
        with patch.object(rotation, "MAX_HISTORY_BYTES", first_size):
            with self.assertRaisesRegex(ValueError, "Next generation"): self.rotate()
        self.assertEqual(before, self.path.read_bytes())
