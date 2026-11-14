"""Isolation profiles reject routing drift, rebinds and ambiguous lifecycle state."""
from pathlib import Path
import os
import tempfile
import unittest
import uuid

from integrations.episode_cluster import EpisodeProfile, reserve_cluster, recover_orphan_cluster, require_dependents_closed
from workenv.core import save, read_json
from workenv.process_identity import process_identity


class EpisodeProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="episode-profile-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_independent_slots_bind_both_service_environment_conventions(self):
        one, two = [EpisodeProfile(self.root, str(uuid.uuid4()), slot) for slot in (0, 1)]
        baseline = {"PGHOST": "unrelated-host", "PG_HOST": "other-host", "PGPORT": "9999", "UNRELATED": "preserved"}
        for profile in (one, two):
            env = profile.environment(baseline)
            self.assertEqual(env["PGHOST"], env["PG_HOST"])
            self.assertEqual((env["PGPORT"], env["PG_PORT"], env["PGDATABASE"], env["PG_DATABASE"]),
                             ("5432", "5432", "toolathlon_gym", "toolathlon_gym"))
            self.assertEqual(env["UNRELATED"], "preserved")
        self.assertNotEqual(one.host, two.host)
        self.assertNotEqual(one.data, two.data)
        self.assertEqual(baseline["PGHOST"], "unrelated-host")

    def test_profile_rejects_unreserved_slots_and_noncanonical_resource_ids(self):
        for slot in (-1, 2, True, "0"):
            with self.assertRaises(ValueError): EpisodeProfile(self.root, str(uuid.uuid4()), slot)
        for sid in ("../outside", "", "A" * 32):
            with self.assertRaises(ValueError): EpisodeProfile(self.root, sid, 0)

    def test_serialized_profile_cannot_change_address_owner_or_data_directory(self):
        profile = EpisodeProfile(self.root, str(uuid.uuid4()), 0)
        profile.validate_record(profile.record())
        for field, value in (("host", "127.0.0.1"), ("data_directory", "../outside"), ("database", "postgres"), ("port", 5555)):
            with self.assertRaises(ValueError): profile.validate_record({**profile.record(), field: value})

    def test_unknown_slot_and_resource_id_reuse_do_not_start_or_overwrite_clusters(self):
        with reserve_cluster(self.root, slot=0) as cluster:
            sid = cluster.profile.resource_id
        path = self.root / ".runtime/native-episodes/slot-0.json"
        self.assertEqual(read_json(path)["state"], "stopped")
        with self.assertRaises(FileExistsError):
            with reserve_cluster(self.root, slot=0, resource_id=sid): pass
        save(path, {"workspace_owner": str(self.root), "slot": 0, "state": "unknown"})
        with self.assertRaises(RuntimeError):
            with reserve_cluster(self.root, slot=0): pass

    def test_changed_owner_marker_refuses_cluster_commands_before_any_process_runs(self):
        with self.assertRaises(ValueError):
            with reserve_cluster(self.root, slot=1) as cluster:
                save(cluster.profile.base / "cluster_owner.json", {**cluster.profile.record(), "host": "127.0.0.1"})
                cluster.command("pg_ctl", ["stop"])
        slot = read_json(self.root / ".runtime/native-episodes/slot-1.json")
        self.assertEqual(slot["state"], "unknown")

    @unittest.skipUnless(os.name == "nt", "Requires the actual Windows process identity probe")
    def test_recovery_never_removes_the_lease_of_a_live_owner(self):
        with reserve_cluster(self.root, slot=0) as cluster:
            lease = cluster.profile.base.parent / "slot-0.lock"
            before = lease.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "alive or unknown"):
                recover_orphan_cluster(self.root, slot=0, resource_id=cluster.profile.resource_id)
            self.assertEqual(before, lease.read_bytes())

    def test_already_stopped_resource_recovery_is_idempotent_and_preserves_data(self):
        with reserve_cluster(self.root, slot=0) as cluster:
            marker = cluster.profile.base / "cluster_owner.json"
            before = marker.read_bytes()
        for _ in range(2):
            result = recover_orphan_cluster(self.root, slot=0, resource_id=cluster.profile.resource_id)
            self.assertEqual(result["state"], "stopped")
            self.assertTrue(result["data_retained"])
            self.assertEqual(before, marker.read_bytes())

    def test_recovery_refuses_a_rebound_slot_before_changing_any_resource(self):
        with reserve_cluster(self.root, slot=1) as cluster: pass
        slot = self.root / ".runtime/native-episodes/slot-1.json"
        record = read_json(slot)
        save(slot, {**record, "resource_id": str(uuid.uuid4())})
        before = slot.read_bytes()
        with self.assertRaises(ValueError): recover_orphan_cluster(self.root, slot=1, resource_id=cluster.profile.resource_id)
        self.assertEqual(before, slot.read_bytes())

    def test_changed_postmaster_creation_identity_refuses_stop(self):
        with reserve_cluster(self.root, slot=0) as cluster:
            cluster.profile.data.mkdir()
            pidfile = cluster.profile.data / "postmaster.pid"
            pidfile.write_text(str(os.getpid()) + "\n", encoding="ascii")
            cluster.started = True
            cluster.identity = {**process_identity(os.getpid()), "creation_id": "different-unit-identity"}
            try:
                with self.assertRaisesRegex(RuntimeError, "PID identity changed"): cluster.stop()
            finally:
                pidfile.unlink()  # A temporary fixture, never a real PG PID file.
                cluster.started = False

    def test_unresolved_dependent_processes_keep_the_slot_unknown_after_database_cleanup(self):
        with self.assertRaisesRegex(RuntimeError, "dependent"):
            with reserve_cluster(self.root, slot=0) as cluster:
                marker = cluster.profile.base / "dependent_processes.json"
                save(marker, {"schema_version": "native-episode-dependents-v1", "profile": cluster.profile.record(),
                    "state": "unknown", "expected_children": ["unit-child"], "children": {}})
                original = marker.read_bytes()
        slot = self.root / ".runtime/native-episodes/slot-0.json"
        self.assertEqual(read_json(slot)["state"], "unknown")
        self.assertEqual(marker.read_bytes(), original)
        with self.assertRaises(RuntimeError):
            with reserve_cluster(self.root, slot=0): self.fail("An unknown child released its slot")
        before = slot.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "dependent"):
            recover_orphan_cluster(self.root, slot=0, resource_id=cluster.profile.resource_id)
        self.assertEqual(slot.read_bytes(), before)

    def test_confirmed_child_closure_allows_release_but_rebound_or_partial_records_do_not(self):
        with reserve_cluster(self.root, slot=0) as cluster:
            profile = cluster.profile
            child = {"profile": profile.record(), "closed": True,
                "before": {"pid": 1234, "status": "alive", "creation_id": "fixture-child"},
                "after": {"pid": 1234, "status": "not_found", "creation_id": None}}
            marker = profile.base / "dependent_processes.json"
            record = {"schema_version": "native-episode-dependents-v1", "profile": profile.record(),
                "state": "closed", "expected_children": ["unit-child"], "children": {"unit-child": child}}
            save(marker, record)
            require_dependents_closed(profile)
            for changes in ({"profile": {}}, {"state": "open"}, {"children": {}}, {"expected_children": []}):
                save(marker, {**record, **changes})
                with self.assertRaisesRegex(RuntimeError, "dependent"): require_dependents_closed(profile)
            save(marker, record)
        self.assertEqual(read_json(self.root / ".runtime/native-episodes/slot-0.json")["state"], "stopped")
        with reserve_cluster(self.root, slot=0): pass

    def test_legacy_unknown_child_marker_blocks_release_and_cannot_be_bypassed_by_stopped_slot_state(self):
        with self.assertRaisesRegex(RuntimeError, "dependent"):
            with reserve_cluster(self.root, slot=1) as cluster:
                marker = cluster.profile.base / "calendar-process-unknown.json"
                marker.write_bytes(b"unknown legacy fixture")
        slotfile = self.root / ".runtime/native-episodes/slot-1.json"
        save(slotfile, {**read_json(slotfile), "state": "stopped"})
        before = slotfile.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "dependent"):
            with reserve_cluster(self.root, slot=1): self.fail("A stopped flag bypassed uncertain child ownership")
        self.assertEqual(slotfile.read_bytes(), before)
        self.assertEqual(marker.read_bytes(), b"unknown legacy fixture")

    def test_declared_closed_child_without_a_real_exit_or_pid_change_is_rejected(self):
        with reserve_cluster(self.root, slot=0) as cluster:
            profile = cluster.profile
            marker = profile.base / "dependent_processes.json"
            child = {"profile": profile.record(), "closed": True,
                "before": {"pid": 1234, "status": "alive", "creation_id": "fixture-child"},
                "after": {"pid": 1234, "status": "alive", "creation_id": "fixture-child"}}
            save(marker, {"schema_version": "native-episode-dependents-v1", "profile": profile.record(),
                "state": "closed", "expected_children": ["unit-child"], "children": {"unit-child": child}})
            with self.assertRaisesRegex(RuntimeError, "dependent"): require_dependents_closed(profile)
            # Remove only this deliberately invalid temporary fixture before leaving its reservation.
            marker.unlink()

    def assert_exit_observation_rejected(self, after):
        with reserve_cluster(self.root, slot=0) as cluster:
            profile = cluster.profile
            marker = profile.base / "dependent_processes.json"
            before = {"pid": 1234, "status": "alive", "creation_id": "fixture-child"}
            child = {"profile": profile.record(), "closed": True, "before": before, "after": after}
            record = {"schema_version": "native-episode-dependents-v1", "profile": profile.record(),
                "state": "closed", "expected_children": ["unit-child"], "children": {"unit-child": child}}
            save(marker, record)
            try:
                with self.assertRaisesRegex(RuntimeError, "dependent"): require_dependents_closed(profile)
            finally:
                # The temporary reservation must close even when this regression
                # intentionally demonstrates a missing production guard.
                save(marker, {**record, "children": {"unit-child": {**child,
                    "after": {"pid": 1234, "status": "not_found", "creation_id": None}}}})

    def test_exit_observation_cannot_rebind_or_omit_the_original_pid(self):
        for pid in (1235, None, True):
            with self.subTest(pid=pid):
                self.assert_exit_observation_rejected({"pid": pid, "status": "not_found", "creation_id": None})

    def test_unknown_status_with_another_creation_identity_is_not_exit_proof(self):
        self.assert_exit_observation_rejected({"pid": 1234, "status": "unknown", "creation_id": "different-fixture-creation"})
