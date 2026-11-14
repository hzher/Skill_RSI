"""Explicit child-state recovery preserves leases, evidence and unknown outcomes."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations import dependent_reconciliation as recovery
from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from workenv.core import ResourceError, save, read_json


class DependentReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = EpisodeProfile(self.root, "c83309a8-4cfa-46d6-a442-f4ac8e774592", 0)
        self.profile.base.mkdir(parents=True)
        save(self.profile.base / "cluster_owner.json", self.profile.record())
        self.slot = self.profile.base.parent / "slot-0.json"
        save(self.slot, {"workspace_owner": str(self.root), "resource_id": self.profile.resource_id, "slot": 0, "state": "unknown"})
        self.children = {name: {"profile": self.profile.record(), "before": {"pid": pid, "status": "alive", "creation_id": "fixture-child-" + str(pid)}, "closed": False}
            for name, pid in (("howtocook", 10), ("google_calendar", 11))}
        self.record = {"schema_version": "native-episode-dependents-v1", "profile": self.profile.record(), "state": "unknown",
            "expected_children": list(self.children), "children": self.children}
        self.marker = self.profile.base / "dependent_processes.json"
        save(self.marker, self.record)
        probe = patch.object(recovery, "process_identity", side_effect=lambda pid: {"pid": pid, "status": "not_found", "creation_id": None})
        self.probe = probe.start()
        self.addCleanup(probe.stop)

    def reconcile(self, **kwargs):
        return recovery.reconcile_dependents(self.root, resource_id=self.profile.resource_id, slot=0, **kwargs)

    def lease(self, path, kind):
        save(path, {"schema_version": "workenv-lock-v0.1", "workspace_owner": str(self.root),
            "resource": path.relative_to(self.root).as_posix(), "kind": kind, "process": {"pid": 99, "creation_id": "fixture-owner"}})
        return path.read_bytes()

    def test_exited_children_are_archived_and_confirmed_without_releasing_the_slot(self):
        old, slot = self.marker.read_bytes(), self.slot.read_bytes()
        result = self.reconcile()
        self.assertEqual(result["outcome"], "children_confirmed_closed")
        self.assertEqual((result["processes_terminated"], result["requests_replayed"], result["slot_released"]), (0, 0, False))
        self.assertEqual((self.root / result["archives"][0]).read_bytes(), old)
        self.assertEqual(self.slot.read_bytes(), slot)
        require_dependents_closed(self.profile)
        settled = self.marker.read_bytes()
        self.assertEqual(self.reconcile()["outcome"], "already_resolved")
        self.assertEqual(self.marker.read_bytes(), settled)

    def test_live_or_unknown_child_preserves_every_input(self):
        old = self.marker.read_bytes()
        for status in ("alive", "unknown"):
            self.probe.side_effect = lambda pid, status=status: {"pid": pid, "status": status, "creation_id": "fixture-child-" + str(pid)}
            with self.assertRaises(ResourceError): self.reconcile()
            self.assertEqual(self.marker.read_bytes(), old)
            self.assertFalse((self.profile.base / "dependent-record-history").exists())

    def test_reused_child_pid_proves_the_old_identity_exited_without_touching_the_new_process(self):
        self.probe.side_effect = lambda pid: {"pid": pid, "status": "alive", "creation_id": "different-fixture-creation"}
        self.assertEqual(self.reconcile()["outcome"], "children_confirmed_closed")
        require_dependents_closed(self.profile)

    def test_incomplete_inventory_or_rebound_child_is_rejected(self):
        for change in ({"children": {"howtocook": self.children["howtocook"]}}, {"profile": {}},
                       {"children": {**self.children, "howtocook": {**self.children["howtocook"], "before": []}}}):
            save(self.marker, {**self.record, **change})
            old = self.marker.read_bytes()
            with self.assertRaises(ResourceError): self.reconcile()
            self.assertEqual(self.marker.read_bytes(), old)

    def test_rebound_or_boolean_slot_is_rejected_before_process_queries(self):
        original = read_json(self.slot)
        for change in ({"resource_id": "other-resource"}, {"slot": False}):
            save(self.slot, {**original, **change})
            with self.assertRaises(ResourceError): self.reconcile()
        self.probe.assert_not_called()

    def test_stale_flags_require_explicit_option_and_preserve_exact_archives_and_slot_lease(self):
        component = self.profile.base / ".calendar.lock"
        component_raw = self.lease(component, recovery.LOCKS[component.name])
        native = self.profile.base.parent / "slot-0.lock"
        native_raw = self.lease(native, "native_episode_slot")
        unknown = self.profile.base / "calendar-process-unknown.json"
        save(unknown, self.children["google_calendar"])
        unknown_raw = unknown.read_bytes()
        with self.assertRaises(ResourceError): self.reconcile()
        self.assertEqual(component.read_bytes(), component_raw)
        result = self.reconcile(recover_stale_locks=True)
        archived = [(self.root / name).read_bytes() for name in result["archives"]]
        self.assertIn(component_raw, archived)
        self.assertIn(unknown_raw, archived)
        self.assertFalse(component.exists())
        self.assertFalse(unknown.exists())
        self.assertEqual(native.read_bytes(), native_raw)
        self.assertEqual(read_json(self.slot)["state"], "unknown")

    def test_live_or_unknown_lease_owner_keeps_the_lock_and_unknown_record(self):
        component = self.profile.base / ".calendar.lock"
        original = self.lease(component, recovery.LOCKS[component.name])
        before = self.marker.read_bytes()
        for status in ("alive", "unknown"):
            self.probe.side_effect = lambda pid, status=status: {"pid": pid, "status": status if pid == 99 else "not_found", "creation_id": "fixture-owner" if pid == 99 else None}
            with self.assertRaises(ResourceError): self.reconcile(recover_stale_locks=True)
            self.assertEqual(component.read_bytes(), original)
            self.assertEqual(self.marker.read_bytes(), before)

    def test_failed_commit_leaves_the_original_unknown_marker_and_slot_fenced(self):
        before, slot = self.marker.read_bytes(), self.slot.read_bytes()
        with patch.object(recovery, "save", side_effect=OSError("fixture commit failure")):
            with self.assertRaises(OSError): self.reconcile()
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertEqual(self.slot.read_bytes(), slot)
        self.assertEqual(len(list((self.profile.base / "dependent-record-history").iterdir())), 1)
        with self.assertRaises(ResourceError): require_dependents_closed(self.profile)
