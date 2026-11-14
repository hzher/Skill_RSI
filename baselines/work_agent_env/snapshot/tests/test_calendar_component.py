"""Calendar startup never uses a foreign episode, failed reset, or changed build."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from integrations import calendar_component as cal
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile
from workenv.core import save


class CalendarComponentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        source = {}
        for name in cal.BUILD_INPUTS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"source fixture")
            source[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.identity = {"source_commit": "a" * 40, "source_sha256": source,
            "source_objects": {n: hashlib.sha1(b"blob 16\0retained fixture").hexdigest() for n in cal.MEMBERS}}
        self.fingerprint = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        self.base = self.root / ".runtime/calendar-build" / self.fingerprint[:24]
        retained = {}
        for name in {*cal.MEMBERS, "package.json", "package-lock.json", "process_observer.mjs", "build/index.js"}:
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"retained fixture")
            retained[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        save(self.base / "build_owner.json", self.identity)
        self.receipt = {"schema_version": "original-calendar-build-v1", "passed": True, "typecheck_completed": True, **self.identity,
            "original_sources_modified": False, "build_fingerprint": self.fingerprint, "build_ref": self.base.relative_to(self.root).as_posix(),
            "source_sha256": source, "retained_sha256": retained}
        self.profile = EpisodeProfile(self.root, "c26d5615-bd7e-4310-899e-a093eb1db684", 0)
        self.profile.base.mkdir(parents=True)
        save(self.profile.base / "cluster_owner.json", self.profile.record())
        self.cluster = EpisodeCluster(self.profile)

    def test_valid_build_and_replaced_or_extra_compiled_members(self):
        self.assertEqual(cal.verify_build(self.root, self.receipt), self.base)
        path = self.base / "build/index.js"
        original = path.read_bytes()
        path.write_bytes(b"changed")
        with self.assertRaises(ValueError): cal.verify_build(self.root, self.receipt)
        path.write_bytes(original)
        (self.base / "build/extra.js").write_bytes(b"extra")
        with self.assertRaises(ValueError): cal.verify_build(self.root, self.receipt)

    def test_semantic_typecheck_and_input_bindings_cannot_be_downgraded(self):
        for changes in ({"typecheck_completed": False}, {"original_sources_modified": True}, {"source_sha256": {}}, {"build_ref": "configs"}):
            with self.assertRaises(ValueError): cal.verify_build(self.root, {**self.receipt, **changes})
        path = self.root / cal.BUILD_INPUTS[0]
        path.write_bytes(b"changed")
        with self.assertRaises(ValueError): cal.verify_build(self.root, self.receipt)

    def test_missing_calendar_schema_blocks_before_original_preprocess_runs(self):
        with patch.object(cal, "require_schema", side_effect=ValueError("missing schema")), patch.object(cal.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "missing schema"): cal.initialize(self.root, self.receipt, self.cluster)
            run.assert_not_called()

    def test_failed_reset_invalidates_prior_ready_marker_and_blocks_mcp(self):
        marker = self.profile.base / "calendar-initialized.json"
        save(marker, {"state": "ready", "profile": self.profile.record(), "build_fingerprint": self.fingerprint})
        failed = subprocess.CompletedProcess([], 1, b"failed", b"injected original error")
        with patch.object(cal, "require_schema", return_value=0), patch.object(cal.subprocess, "run", return_value=failed):
            with self.assertRaises(RuntimeError): cal.initialize(self.root, self.receipt, self.cluster)
            self.assertEqual(json.loads(marker.read_text())["state"], "initializing")
            async def attempt():
                async with cal.calendar_session(self.root, self.receipt, self.cluster, None):
                    self.fail("Failed reset must not reach an MCP session")
            with self.assertRaises(ValueError): asyncio.run(attempt())

    def test_episode_or_build_rebind_blocks_before_mcp_import(self):
        async def attempt():
            async with cal.calendar_session(self.root, self.receipt, self.cluster, None): self.fail("Rebound episode accepted")
        marker = self.profile.base / "calendar-initialized.json"
        for changes in ({"profile": {}}, {"build_fingerprint": "other"}):
            save(marker, {"state": "ready", "profile": self.profile.record(), "build_fingerprint": self.fingerprint, **changes})
            with patch.object(cal, "require_schema", return_value=0):
                with self.assertRaises(ValueError): asyncio.run(attempt())

    def test_foreign_cluster_cannot_initialize(self):
        with self.assertRaises(ValueError): cal.initialize(self.root / "other", self.receipt, self.cluster)

    def test_schema_requires_exact_column_types_and_closes_connection(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = list(cal.COLUMNS.items())
        cur.fetchone.return_value = (0,)
        with patch.object(self.cluster, "connect", return_value=conn):
            self.assertEqual(cal.require_schema(self.cluster), 0)
            conn.close.assert_called_once()
            conn.close.reset_mock()
            cur.fetchall.return_value = [("id", "integer")]
            with self.assertRaises(ValueError): cal.require_schema(self.cluster)
            conn.close.assert_called_once()
