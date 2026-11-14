import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from integrations.resource_ledger import ResourceLedger


class ResourceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "resource-sessions.json"
        self.now = 1000.0
        self.ledger = ResourceLedger(self.path, self.root, ttl_seconds=10, clock=lambda: self.now, monotonic_clock=lambda: self.now)
        self.sid = str(uuid.uuid4())
        self.binding = {"resources_session_id": self.sid, "episode_id": {"rollout_id": "unit-only", "attempt": 0}}

    def test_closed_identity_survives_restart_and_cannot_be_reopened(self):
        self.ledger.close_unknown(self.sid, self.binding)
        after = ResourceLedger(self.path, self.root)
        self.assertEqual(after.rows[self.sid]["state"], "closed")
        with self.assertRaises(ValueError): after.seed(self.sid, self.binding)
        with self.assertRaises(ValueError): after.update(self.sid, state="active")

    def test_active_lease_expires_and_clock_rollback_expires(self):
        self.ledger.seed(self.sid, self.binding)
        self.ledger.update(self.sid, state="active", run_ref="runs/unit")
        self.now = 1009
        self.assertEqual(self.ledger.expired(), [])
        self.now = 1010
        self.assertEqual(self.ledger.expired(), [self.sid])
        self.now = 990
        self.assertEqual(self.ledger.expired(), [self.sid])

    def test_touch_renews_resource_lease(self):
        self.ledger.seed(self.sid, self.binding)
        self.ledger.update(self.sid, state="active")
        self.now = 1008
        self.ledger.update(self.sid, state="active")
        self.now = 1012
        self.assertEqual(self.ledger.expired(), [])

    def test_live_lease_ignores_wall_clock_corrections_but_expires_on_elapsed_time(self):
        wall, elapsed = [1000.0], [50.0]
        ledger = ResourceLedger(self.path, self.root, ttl_seconds=10, clock=lambda: wall[0], monotonic_clock=lambda: elapsed[0])
        ledger.seed(self.sid, self.binding)
        ledger.update(self.sid, state="active")
        for corrected in (998.8, 5000.0):
            wall[0], elapsed[0] = corrected, 59.9
            self.assertEqual(ledger.expired(), [])
        elapsed[0] = 60.0
        self.assertEqual(ledger.expired(), [self.sid])

    def test_restart_does_not_grant_a_fresh_monotonic_lease(self):
        self.ledger.seed(self.sid, self.binding)
        self.ledger.update(self.sid, state="active")
        for wall in (990, 1010):
            restored = ResourceLedger(self.path, self.root, ttl_seconds=10, clock=lambda: wall, monotonic_clock=lambda: 1.0)
            self.assertEqual(restored.expired(), [self.sid])

    def test_failed_touch_does_not_extend_live_deadline(self):
        self.ledger.seed(self.sid, self.binding)
        self.ledger.update(self.sid, state="active")
        self.now = 1008
        with patch.object(self.ledger, "save", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError): self.ledger.update(self.sid, state="active")
        self.now = 1010
        self.assertEqual(self.ledger.expired(), [self.sid])

    def test_failed_persistence_does_not_claim_close_or_forget_session(self):
        self.ledger.seed(self.sid, self.binding)
        self.ledger.update(self.sid, state="active")
        with patch.object(self.ledger, "save", side_effect=PermissionError("unit fixture")):
            with self.assertRaises(PermissionError): self.ledger.update(self.sid, state="closed")
        self.assertEqual(self.ledger.rows[self.sid]["state"], "active")
        after = ResourceLedger(self.path, self.root)
        self.assertEqual(after.rows[self.sid]["state"], "active")

    def test_capacity_keeps_tombstones_and_rejects_new_ids(self):
        ledger = ResourceLedger(self.path, self.root, capacity=1)
        ledger.close_unknown(self.sid, self.binding)
        with self.assertRaises(ValueError): ledger.seed(str(uuid.uuid4()), self.binding)
        self.assertEqual(len(ledger.rows), 1)

    def test_owner_mismatch_and_corruption_fail_closed(self):
        self.ledger.close_unknown(self.sid, self.binding)
        value = json.loads(self.path.read_text())
        value["owner"] = "different-workspace"
        self.path.write_text(json.dumps(value))
        with self.assertRaises(ValueError): ResourceLedger(self.path, self.root)
        self.path.write_text("{")
        with self.assertRaises(ValueError): ResourceLedger(self.path, self.root)


if __name__ == "__main__": unittest.main()
