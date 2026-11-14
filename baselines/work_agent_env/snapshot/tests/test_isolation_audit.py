import hashlib
from pathlib import Path
import tempfile
import unittest

from integrations.isolation_audit import evidence_binding


class IsolationEvidenceTests(unittest.TestCase):
    def test_changed_retained_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / ".runtime/native-episodes/unit-only/stdout.txt"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"UNIT DIAGNOSTIC FIXTURE\n")
            hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()}
            self.assertTrue(evidence_binding(root, hashes))
            path.write_bytes(b"CHANGED FIXTURE\n")
            with self.assertRaisesRegex(ValueError, "changed"): evidence_binding(root, hashes)

    def test_other_runtime_private_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaises(ValueError): evidence_binding(root, {".runtime/unrelated-private.json": "a" * 64})
            with self.assertRaises(ValueError): evidence_binding(root, {".runtime/native-episodes/../../private.json": "a" * 64})
