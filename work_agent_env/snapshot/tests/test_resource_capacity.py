from pathlib import Path
import tempfile
import unittest
import uuid

from integrations.resource_capacity import capacity_report
from integrations.resource_ledger import ResourceLedger


class ResourceCapacityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="resource-capacity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def ledger(self, filename, count):
        ledger = ResourceLedger(self.root / ".runtime/nemo-vm" / filename, self.root, clock=lambda: 1000)
        for _ in range(count):
            sid = str(uuid.uuid4())
            ledger.seed(sid, {"episode_id": {"rollout_id": "unit"}})
            ledger.update(sid, state="closed")
        if not count: ledger.save()
        return ledger

    def test_missing_ledgers_do_not_create_files_or_claim_runtime_ready(self):
        result = capacity_report(self.root)
        self.assertFalse(result["fits_observed_capacity"])
        self.assertFalse((self.root / ".runtime").exists())

    def test_retained_tombstones_reduce_new_identity_budget_without_mutation(self):
        original = self.ledger("resource-sessions.json", 2)
        synthetic = self.ledger("synthetic-sessions.json", 5)
        before = [p.path.read_bytes() for p in (original, synthetic)]
        result = capacity_report(self.root, required_native=2, required_synthetic=40, clock=lambda: 1001)
        self.assertTrue(result["fits_observed_capacity"])
        self.assertEqual(result["sources"]["project_synthetic"]["remaining_new_identities"], 123)
        self.assertEqual(before, [p.path.read_bytes() for p in (original, synthetic)])
        self.assertFalse(result["reserves_resources"])

    def test_insufficient_budget_or_all_active_workers_reject_new_work(self):
        original = self.ledger("resource-sessions.json", 0)
        self.ledger("synthetic-sessions.json", 0)
        sid = str(uuid.uuid4())
        original.seed(sid, {"episode_id": {"rollout_id": "unit"}})
        original.update(sid, state="active")
        self.assertFalse(capacity_report(self.root, required_native=1)["fits_observed_capacity"])
        original.update(sid, state="closed")
        self.assertFalse(capacity_report(self.root, required_native=128)["fits_observed_capacity"])

    def test_clock_rollback_is_visible_without_inventing_lease_expiry(self):
        self.ledger("resource-sessions.json", 1)
        self.ledger("synthetic-sessions.json", 1)
        result = capacity_report(self.root, clock=lambda: 999)
        self.assertEqual(result["sources"]["original_toolathlon"]["clock_rollback_rows"], 1)
        self.assertTrue(result["sources"]["original_toolathlon"]["lease_expiry_not_inferred"])
        for value in (-1, True, 129, "1"):
            with self.assertRaises(ValueError): capacity_report(self.root, required_native=value)
