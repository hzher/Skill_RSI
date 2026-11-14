"""Reject fabricated email success, partial-write evidence and dependency changes."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from integrations import email_audit as audit
from integrations import email_build as build
from integrations import email_components as email
from integrations.episode_cluster import EpisodeProfile
from tests.test_email_components import ARGS, OK, empty, sent, response
from workenv.core import ResourceError, save


class EmailJournalAuditTests(unittest.TestCase):
    def test_inventory_journal_requires_explicit_task_and_sender(self):
        from integrations.email_task_identity import EmailTaskIdentity
        spec = EmailTaskIdentity('wc-inventory-alert-gcal')
        self.write()
        with self.assertRaisesRegex(ValueError, 'task or sender'):
            audit.audit_journal(self.root, self.profile, self.retained, task_id=spec.task_id)
        path = self.run / 'requests.json'; journal = json.loads(path.read_text())
        journal.update(task_id=spec.task_id, sender=spec.sender); save(path, journal)
        after = sent(); after['email']['messages'][0]['from_addr'] = spec.sender
        save(self.root / self.entry['after_ref'], after)
        known, unknown = audit.audit_journal(self.root, self.profile, self.retained, task_id=spec.task_id)
        self.assertEqual((len(known), len(unknown)), (1, 0))
        journal['sender'] = 'vip-program@store.example.com'; save(path, journal)
        with self.assertRaisesRegex(ValueError, 'task or sender'):
            audit.audit_journal(self.root, self.profile, self.retained, task_id=spec.task_id)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.profile = EpisodeProfile(self.root, str(uuid4()), 0)
        self.run = self.profile.base / "email-components-run"; self.run.mkdir(parents=True)
        self.identity = str(uuid4()); self.retained = {}
        self.entry = {"name": "send_email", "arguments": ARGS, "fingerprint": email.fingerprint("send_email", ARGS),
                      "state": "committed", "response": deepcopy(OK)}
        for phase, data in (("before", empty()), ("after", sent())):
            path = self.run / (self.identity + "." + phase + ".json"); save(path, data)
            name = path.relative_to(self.root).as_posix(); self.entry[phase + "_ref"] = name
            self.retained[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    def write(self):
        save(self.run / "requests.json", {"schema_version": "owned-email-request-journal-v1", "profile": self.profile.record(),
            "entries": {self.identity: self.entry}})

    def check(self):
        self.write(); return audit.audit_journal(self.root, self.profile, self.retained)

    def test_original_effect_recomputed_instead_of_trusting_state(self):
        known, unknown = self.check(); self.assertEqual((len(known), len(unknown)), (1, 0))
        data = sent(); data["email"]["messages"][0]["body_text"] = "tampered"
        save(self.root / self.entry["after_ref"], data)
        with self.assertRaises(ResourceError): self.check()

    def test_request_parameter_change_cannot_reuse_old_fingerprint(self):
        self.entry["arguments"] = {**ARGS, "body": "changed"}
        with self.assertRaisesRegex(ValueError, "fingerprint"): self.check()

    def test_snapshot_rebinding_to_another_resource_is_rejected(self):
        self.entry["before_ref"] = "evidence/another.json"
        with self.assertRaisesRegex(ValueError, "rebound"): self.check()

    def test_partial_commit_requires_exact_message_and_missing_log(self):
        self.entry.update(state="unknown", error_type="ResourceError", response=response("Error sending email: fixture"))
        data = sent(); data["email"]["sent_log"] = []
        save(self.root / self.entry["after_ref"], data)
        known, unknown = self.check(); self.assertEqual((len(known), len(unknown)), (0, 1))
        data["email"]["messages"][0]["to_addr"] = ["wrong@example.com"]
        save(self.root / self.entry["after_ref"], data)
        with self.assertRaises(ResourceError): self.check()

    def test_success_cannot_be_relabelled_unknown_to_mask_a_failure(self):
        self.entry.update(state="unknown", error_type="ResourceError")
        with self.assertRaisesRegex(ValueError, "no failed observation"): self.check()

    def test_component_summary_cannot_claim_reward_or_activation(self):
        path = self.root / "evidence/result.json"
        for field, value in (("reward", 1), ("runtime_task_activated", True), ("external_messages_sent", 1)):
            save(path, {"schema_version": "owned-email-components-diagnostic-v1", "passed": True,
                "checks": [{"name": c, "passed": True} for c in audit.CHECKS], field: value})
            with self.subTest(field=field), self.assertRaises(ValueError): audit.audit_components(self.root, path)


class EmailBuildIntegrityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in build.BUILD_INPUTS:
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"fixture")
        hashes = {n: build.digest(self.root / n) for n in build.BUILD_INPUTS}
        self.original = b"# original package fixture\n"
        objects = {build.PREFIX + "src/emails_mcp/__init__.py": {"sha256": hashlib.sha256(self.original).hexdigest()}}
        identity = {"source_commit": build.PIN, "source_objects": objects, "source_sha256": hashes}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self.base = self.root / ".runtime/email-build" / fingerprint[:24]; self.base.mkdir(parents=True)
        save(self.base / "build_owner.json", identity)
        source = self.base / "src/emails_mcp/__init__.py"; source.parent.mkdir(parents=True); source.write_bytes(self.original)
        self.env = self.root / ".runtime/email-environments" / hashes["configs/email.requirements.lock.txt"][:24]
        self.env.mkdir(parents=True)
        for i in range(11): (self.env / ("dependency" + str(i))).write_bytes(b"locked")
        save(self.env / "environment_manifest.json", {"files": build.environment_files(self.env)})
        self.record = {"schema_version": "original-email-pg-build-v1", "passed": True, **identity, "syntax_checked": True,
            "original_sources_modified": False, "build_ref": self.base.relative_to(self.root).as_posix(), "build_fingerprint": fingerprint,
            "environment_ref": self.env.relative_to(self.root).as_posix(), "environment_manifest_sha256": build.digest(self.env / "environment_manifest.json"),
            "retained_sha256": {"src/emails_mcp/__init__.py": hashlib.sha256(self.original).hexdigest()}}

    def test_original_source_and_environment_bytes_verified(self):
        self.assertEqual(build.verify_build(self.root, self.record, check_environment=True)[0], self.base)
        (self.env / "dependency0").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "dependency bytes"): build.verify_build(self.root, self.record, check_environment=True)

    def test_unexpected_module_or_bytecode_is_rejected(self):
        (self.base / "src/emails_mcp/extra.py").write_text("raise RuntimeError('injected')")
        with self.assertRaisesRegex(ValueError, "Unexpected module"): build.verify_build(self.root, self.record)

    def test_changed_original_file_or_owner_is_rejected(self):
        source = self.base / "src/emails_mcp/__init__.py"; source.write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "source bytes"): build.verify_build(self.root, self.record)
        source.write_bytes(self.original); save(self.base / "build_owner.json", {})
        with self.assertRaisesRegex(ValueError, "owner differs"): build.verify_build(self.root, self.record)

    def test_dependency_manifest_change_and_foreign_build_path_are_rejected(self):
        (self.env / "environment_manifest.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "environment identity"): build.verify_build(self.root, self.record)
        wrong = {**self.record, "build_ref": "evidence/foreign"}
        with self.assertRaises(ValueError): build.verify_build(self.root, wrong)
