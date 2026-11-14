"""Owned file fixtures; no PG, MCP, network or original preprocess execution."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from integrations import email_task_identity as identity
from integrations.episode_cluster import EpisodeProfile, EpisodeCluster
from adapters.email_pg_native import SENDER as VIP_SENDER
from adapters.inventory_alert_validation import SENDER as INVENTORY_SENDER
from workenv.core import ResourceError, save


class EmailTaskIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = EpisodeProfile(self.root, str(uuid4()), 0)
        self.profile.base.mkdir(parents=True)
        save(self.profile.base / "cluster_owner.json", self.profile.record())
        self.cluster = EpisodeCluster(self.profile)
        self.cluster.started = True  # Explicit file-only fixture; no PG was started.
        self.build = {"passed": True, "build_fingerprint": "b" * 64}
        self.source_patch = patch.object(identity, "_preprocess", side_effect=lambda root, spec: self.source(spec.task_id))
        self.source_patch.start(); self.addCleanup(self.source_patch.stop)
        self.build_patch = patch.object(identity, "verify_build", return_value=(self.root, self.root / "fixture-python"))
        self.verify_build = self.build_patch.start(); self.addCleanup(self.build_patch.stop)
        for task_id in identity.TASKS: self.write_marker(task_id)

    def source(self, task):
        return {"checkout": ".upstream/fixture", "commit": identity.vip.PIN,
                "path": "tasks/finalpool/" + task + "/preprocess/main.py", "git_blob": "a" * 40, "sha256": "c" * 64}

    def marker(self, task): return self.profile.base / identity.EmailTaskIdentity(task).marker

    def write_marker(self, task):
        value = {"state": "ready", "task_id": task, "profile": self.profile.record(),
                 "preprocess_source": self.source(task), "readonly_sha256": "d" * 64}
        if task == identity.inventory.TASK_ID: value["contract_source"] = identity.inventory_contract()
        save(self.marker(task), value)
        return value

    def journal(self, *, task=None, state="committed", scoped=True):
        task = task or identity.inventory.TASK_ID
        value = {"schema_version": "owned-email-request-journal-v1", "profile": self.profile.record(),
                 "entries": {str(uuid4()): {"state": state}}}
        if scoped: value.update(task_id=task, sender=identity.EmailTaskIdentity(task).sender)
        path = self.profile.base / "email-components-run/requests.json"
        save(path, value)
        return path, value

    def launch(self, task=identity.inventory.TASK_ID):
        return identity.make_launch(self.root, self.cluster, self.build, task_id=task)

    def process(self, launch):
        return {"schema_version": "owned-email-task-process-v1", "profile": self.profile.record(),
                "task_binding": deepcopy(launch["task_binding"]), "methods": list(identity.METHODS),
                "backend_classes": list(identity.BACKENDS), "pg_data_ref": self.profile.data.relative_to(self.root).as_posix(),
                "build_fingerprint": self.build["build_fingerprint"], "forbidden_socket_blocked": True,
                "external_messages_sent": 0, "process": {"pid": 1234, "status": "alive", "creation_id": "fixture-birth"}}

    def test_fixed_addresses_match_current_vip_and_inventory_contracts(self):
        self.assertEqual(identity.EmailTaskIdentity().sender, VIP_SENDER)
        self.assertEqual(identity.EmailTaskIdentity(identity.inventory.TASK_ID).sender, INVENTORY_SENDER)
        self.assertEqual(len(identity.TASKS), 2)

    def test_arbitrary_sender_and_identity_mutation_are_not_supported(self):
        spec = identity.EmailTaskIdentity()
        with self.assertRaises(TypeError): identity.EmailTaskIdentity(sender="external@example.com")
        with self.assertRaises(FrozenInstanceError): spec.task_id = identity.inventory.TASK_ID
        with self.assertRaises(TypeError): identity.TASKS["new"] = ("x", "y")

    def test_only_exact_task_ids_are_accepted(self):
        for value in (None, "", "unknown", True, " " + identity.vip.TASK_ID, identity.inventory.TASK_ID.upper()):
            with self.subTest(value=value), self.assertRaises(ValueError): identity.EmailTaskIdentity(value)

    def test_each_task_attests_its_own_fixed_marker_without_mutation(self):
        for task in identity.TASKS:
            path = self.marker(task); before = path.read_bytes()
            observed = identity.attest_initialization(self.root, self.cluster, task_id=task)
            self.assertEqual(observed["marker_sha256"], hashlib.sha256(before).hexdigest())
            self.assertEqual(observed["sender"], identity.EmailTaskIdentity(task).sender)
            self.assertEqual(path.read_bytes(), before)

    def test_closed_or_foreign_cluster_is_rejected_before_build(self):
        self.cluster.started = False
        with self.assertRaises(ResourceError): self.launch()
        self.verify_build.assert_not_called()
        self.cluster.started = True
        with tempfile.TemporaryDirectory() as other, self.assertRaises(ResourceError):
            identity.attest_initialization(Path(other).resolve(), self.cluster)

    def test_owner_file_cannot_be_rebound(self):
        save(self.profile.base / "cluster_owner.json", {**self.profile.record(), "slot": 1})
        with self.assertRaises(ValueError): identity.attest_initialization(self.root, self.cluster)

    def test_marker_state_task_profile_and_source_are_all_checked(self):
        task = identity.inventory.TASK_ID
        for field, value in (("state", "initializing"), ("task_id", identity.vip.TASK_ID), ("profile", {}),
                             ("preprocess_source", self.source(identity.vip.TASK_ID)), ("readonly_sha256", "unknown")):
            marker = self.write_marker(task); marker[field] = value; save(self.marker(task), marker)
            with self.subTest(field=field), self.assertRaises(ResourceError):
                identity.attest_initialization(self.root, self.cluster, task_id=task)

    def test_inventory_contract_and_optional_sender_cannot_be_rebound(self):
        task = identity.inventory.TASK_ID
        for field, value in (("contract_source", {}), ("sender", VIP_SENDER)):
            marker = self.write_marker(task); marker[field] = value; save(self.marker(task), marker)
            with self.subTest(field=field), self.assertRaises(ResourceError):
                identity.attest_initialization(self.root, self.cluster, task_id=task)

    def test_missing_oversized_duplicate_nonfinite_and_nonobject_markers_fail(self):
        path = self.marker(identity.vip.TASK_ID)
        path.unlink()
        with self.assertRaises(ResourceError): identity.attest_initialization(self.root, self.cluster)
        for raw in (b"x" * 65537, b'{"state":"ready","state":"unknown"}', b'{"value":NaN}', b"[]"):
            path.write_bytes(raw)
            with self.subTest(raw_length=len(raw)), self.assertRaises((ValueError, ResourceError)):
                identity.attest_initialization(self.root, self.cluster)

    def test_linked_marker_is_rejected(self):
        path = self.marker(identity.vip.TASK_ID)
        target = self.profile.base / "source.json"; target.write_bytes(path.read_bytes()); path.unlink()
        try: path.symlink_to(target)
        except OSError: self.skipTest("Windows cannot create symlink; Linux executes this guard")
        with self.assertRaises(ValueError): identity.attest_initialization(self.root, self.cluster)

    def test_unresolved_requests_block_initialization_without_changing_journal(self):
        for state in ("prepared", "unknown"):
            path, _ = self.journal(state=state); before = path.read_bytes()
            with self.subTest(state=state), self.assertRaises(ResourceError): self.launch()
            self.assertEqual(path.read_bytes(), before)

    def test_inventory_cannot_borrow_a_legacy_vip_journal(self):
        self.journal(task=identity.vip.TASK_ID, scoped=False)
        identity.attest_initialization(self.root, self.cluster)
        with self.assertRaisesRegex(ResourceError, "another task"): self.launch()

    def test_scoped_committed_journal_requires_task_sender_and_profile(self):
        path, journal = self.journal()
        self.launch()
        for field, value in (("task_id", identity.vip.TASK_ID), ("sender", VIP_SENDER), ("profile", {})):
            changed = {**journal, field: value}; save(path, changed)
            with self.subTest(field=field), self.assertRaises(ResourceError): self.launch()

    def test_journal_schema_count_and_request_uuid_are_bounded(self):
        path, journal = self.journal()
        cases = [{**journal, "schema_version": "other"}, {**journal, "entries": []},
                 {**journal, "entries": {"not-a-uuid": {"state": "committed"}}},
                 {**journal, "entries": {str(uuid4()): {"state": "committed"} for _ in range(129)}}]
        for value in cases:
            save(path, value)
            with self.assertRaises((ValueError, ResourceError)): self.launch()

    def test_launch_delegates_build_validation_and_freezes_build_bytes(self):
        launch = self.launch()
        self.verify_build.assert_called_once_with(self.root, self.build)
        self.assertEqual(launch["task_binding"]["sender"], INVENTORY_SENDER)
        self.build["passed"] = False
        self.assertTrue(launch["build"]["passed"])
        self.verify_build.side_effect = ValueError("changed build")
        with self.assertRaisesRegex(ValueError, "changed build"): self.launch()

    def test_legacy_launch_and_cross_task_launch_are_rejected(self):
        with self.assertRaises(ResourceError):
            identity.validate_launch(self.root, self.cluster, {"profile": self.profile.record(), "build": self.build})
        launch = self.launch(identity.vip.TASK_ID)
        with self.assertRaises(ResourceError):
            identity.validate_launch(self.root, self.cluster, launch, task_id=identity.inventory.TASK_ID)

    def test_launch_cannot_hide_changed_build_profile_sender_or_extra_fields(self):
        launch = self.launch()
        cases = []
        changed = deepcopy(launch); changed["build"]["build_fingerprint"] = "e" * 64; cases.append(changed)
        changed = deepcopy(launch); changed["task_binding"]["sender"] = VIP_SENDER; cases.append(changed)
        cases.extend(({**launch, "profile": {}}, {**launch, "extra": True}))
        for value in cases:
            with self.assertRaises(ResourceError):
                identity.validate_launch(self.root, self.cluster, value, task_id=identity.inventory.TASK_ID)

    def test_same_marker_contents_with_different_bytes_invalidate_old_launch(self):
        launch = self.launch()
        path = self.marker(identity.inventory.TASK_ID)
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(ResourceError):
            identity.validate_launch(self.root, self.cluster, launch, task_id=identity.inventory.TASK_ID)

    def test_process_requires_exact_live_birth_identity(self):
        launch = self.launch(); observed = self.process(launch)
        with patch.object(identity, "process_identity", return_value=observed["process"]):
            result = identity.validate_process(self.root, self.cluster, launch, observed, task_id=identity.inventory.TASK_ID)
            self.assertEqual(result, launch["task_binding"])
        for actual in ({"pid": 1234, "status": "not_found"}, {"pid": 1234, "status": "alive", "creation_id": "reused"}):
            with patch.object(identity, "process_identity", return_value=actual), self.assertRaises(ResourceError):
                identity.validate_process(self.root, self.cluster, launch, observed, task_id=identity.inventory.TASK_ID)

    def test_process_cannot_change_transport_task_profile_or_noninteger_counters(self):
        launch = self.launch(); observed = self.process(launch)
        for field, value in (("task_binding", {}), ("profile", {}), ("pg_data_ref", "other"), ("backend_classes", []),
                             ("forbidden_socket_blocked", False), ("external_messages_sent", False), ("methods", []),
                             ("build_fingerprint", "e" * 64), ("process", {"pid": True, "status": "alive", "creation_id": "x"})):
            changed = {**observed, field: value}
            with self.subTest(field=field), patch.object(identity, "process_identity") as probe, self.assertRaises(ResourceError):
                identity.validate_process(self.root, self.cluster, launch, changed, task_id=identity.inventory.TASK_ID)
            probe.assert_not_called()

    def review_fixture(self):
        for name in identity.SOURCES:
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# File-only source fixture\n", encoding="utf-8")
        (self.root / "adapters/email_pg_native.py").write_text('SENDER="' + VIP_SENDER + '"\nraise RuntimeError("never execute")\n')

    def test_review_parses_legacy_source_without_executing_it(self):
        self.review_fixture()
        report = identity.describe(self.root)
        self.assertFalse(report["runtime_integrated"])
        self.assertFalse(report["original_email_tools_called"])
        self.assertEqual(len(report["tasks"]), 2)
        save(self.root / identity.REPORT, report)
        self.assertEqual(identity.audit_review(self.root, self.root / identity.REPORT)["new_runtime_profiles"], 0)

    def test_forged_tool_activation_task_reward_or_source_coverage_is_rejected(self):
        self.review_fixture(); original = identity.describe(self.root)
        for field, value in (("runtime_integrated", True), ("original_email_tools_called", True), ("reward", 1), ("source_sha256", {})):
            save(self.root / identity.REPORT, {**original, field: value})
            with self.subTest(field=field), self.assertRaises(ValueError): identity.audit_review(self.root, self.root / identity.REPORT)

    def test_runtime_import_is_reported_without_claiming_actual_tools(self):
        self.review_fixture()
        for statement in ('from integrations.email_task_identity import make_launch', 'from .email_task_identity import make_launch',
                          'from integrations import email_task_identity', 'from . import email_task_identity',
                          'import integrations.email_task_identity as identity'):
            (self.root / "integrations/email_components.py").write_text(statement + '\n')
            with self.subTest(statement=statement):
                report = identity.describe(self.root)
                self.assertTrue(report["legacy_runtime_imports_contract"]["integrations/email_components.py"])
                self.assertFalse(report["original_email_tools_called"])
                self.assertFalse(report["runtime_integrated"])

    def test_upstream_preprocess_selection_is_fixed_for_each_task(self):
        # Exercise the real selector, not the file-fixture patch used above.
        self.source_patch.stop()
        for task, module, path in ((identity.vip.TASK_ID, identity.vip, identity.vip.BASE + "preprocess/main.py"),
                                   (identity.inventory.TASK_ID, identity.inventory, "preprocess/main.py")):
            with patch.object(module, "public_blob", return_value=(b"not executed", self.source(task))) as read:
                self.assertEqual(identity._preprocess(self.root, identity.EmailTaskIdentity(task)), self.source(task))
                read.assert_called_once_with(self.root, path)


if __name__ == "__main__": unittest.main()
