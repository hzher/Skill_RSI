"""Public-only source and static-review boundaries, with no task execution."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from training import email_task_review as review
from workenv.core import save

ROOT = Path(__file__).resolve().parents[1]


class EmailPublicReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in review.SOURCES[:3]:
            p = self.root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes((ROOT / name).read_bytes())
        save(self.root / review.SOURCES[3], {"sources": {"toolathlon_gym": {"commit": review.PIN, "checkout": ".upstream/fixture"}}})
        self.config = json.dumps({"needed_mcp_servers": review.SERVICES}).encode()
        save(self.root / review.SOURCES[4], {"source": "toolathlon_gym", "commit": review.PIN, "tasks": {
            review.TASK_ID: {"configuration_status": "inspected", "needed_mcp_servers": review.SERVICES,
                             "config_sha256": hashlib.sha256(self.config).hexdigest()}}})

    def blob_fixture(self, root, name):
        if name.endswith("task_config.json"): raw = self.config
        elif name.endswith("preprocess/main.py"):
            tables = ["email." + n for n in ("messages", "attachments", "drafts", "sent_log")]
            tables += ["gsheet." + n for n in ("cells", "sheets", "permissions", "spreadsheets")]
            raw = ("\n".join("cursor.execute('DELETE FROM " + t + "')" for t in tables)
                   + "\nfrom pathlib import Path\nPath(" + repr(str(self.root / "must-not-execute")) + ").write_text('bad')\n").encode()
        elif name.endswith("evaluation/main.py"): raw = b"cursor.execute('SELECT * FROM wc.customers')\nraise RuntimeError('must not execute')\n"
        elif name.endswith("pg_backend.py"): raw = b"class PgSMTPBackend: pass\n"
        else: raw = b"# explicit public fixture\n"
        return raw, {"path": name, "sha256": hashlib.sha256(raw).hexdigest()}

    def test_unlisted_answer_and_path_reads_fail_before_git(self):
        with patch.object(review, "pinned_blob") as reader:
            for name in (review.BASE + "groundtruth/answer.xlsx", "../private", "tasks/verified/task.md"):
                with self.assertRaises(ValueError): review.public_blob(self.root, name)
            reader.assert_not_called()

    def test_changed_source_pin_fails_before_git(self):
        save(self.root / review.SOURCES[3], {"sources": {"toolathlon_gym": {"commit": "0" * 40}}})
        with patch.object(review, "pinned_blob") as reader:
            with self.assertRaises(ValueError): review.public_blob(self.root, review.BASE + "docs/task.md")
            reader.assert_not_called()

    def test_substituted_blob_bytes_are_rejected(self):
        with patch.object(review, "pinned_blob", return_value=(b"substituted", {})):
            with self.assertRaises(ValueError): review.public_blob(self.root, review.BASE + "docs/task.md")

    def test_metadata_scope_mismatch_is_rejected(self):
        self.config = json.dumps({"needed_mcp_servers": ["emails"]}).encode()
        with patch.object(review, "public_blob", side_effect=self.blob_fixture):
            with self.assertRaises(ValueError): review.inspect_task(self.root)

    def test_static_review_never_executes_task_or_mail(self):
        with patch.object(review, "public_blob", side_effect=self.blob_fixture): report = review.inspect_task(self.root)
        self.assertEqual(len(report["objects"]), 9)
        self.assertFalse((self.root / "must-not-execute").exists())
        for name in ("runtime_activation", "training_admission", "scripts_executed", "email_service_executed", "reference_solutions_read", "verified_task_bodies_read"):
            self.assertIs(report[name], False)
        self.assertIsNone(report["reward"])
        self.assertEqual(report["external_messages_sent"], 0)

    def test_tampered_review_summary_is_rejected(self):
        with patch.object(review, "public_blob", side_effect=self.blob_fixture):
            report = review.inspect_task(self.root)
            report["runtime_activation"] = True
            path = self.root / "evidence/review.json"; save(path, report)
            with self.assertRaisesRegex(ValueError, "summary differs"): review.audit_review(self.root, path)
