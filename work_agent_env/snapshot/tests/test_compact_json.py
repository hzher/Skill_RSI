"""Snapshot layout must preserve data and atomic-write failure guarantees."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workenv.core import save


class CompactJSONTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "snapshot.json"
        self.original = b'{"old":true}\n'
        self.path.write_bytes(self.original)

    def unchanged(self):
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_default_layout_retains_existing_unicode_and_indentation(self):
        value = {"text": "\u4e2d\u6587\nsecond line", "values": [True, None, 1.25]}
        save(self.path, value)
        self.assertEqual(self.path.read_text(encoding="utf-8"), json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def test_compact_snapshot_keeps_nested_values_and_numeric_types(self):
        value = {"rows": [{"id": i, "name": "\u91c7\u8d2d", "price": 1.25, "empty": None,
                            "enabled": True, "text": "line\nwith \"quotes\" and \\slash"} for i in range(30)]}
        save(self.path, value, compact=True)
        raw = self.path.read_bytes()
        restored = json.loads(raw)
        self.assertEqual(restored, value)
        self.assertIs(type(restored["rows"][0]["id"]), int)
        self.assertIs(type(restored["rows"][0]["enabled"]), bool)
        self.assertIs(type(restored["rows"][0]["price"]), float)
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertLess(len(raw), len(json.dumps(value, ensure_ascii=False, indent=2).encode()))

    def test_compact_replace_failure_retains_previous_bytes(self):
        error = OSError("explicit replacement failure")
        with patch("workenv.core.replace_atomic", side_effect=error):
            with self.assertRaises(OSError) as raised:
                save(self.path, {"new": True}, compact=True)
        self.assertIs(raised.exception, error)
        self.unchanged()

    def test_compact_circular_value_retains_previous_bytes(self):
        circular = []
        circular.append(circular)
        with self.assertRaises(ValueError):
            save(self.path, circular, compact=True)
        self.unchanged()

    def test_invalid_layout_option_does_not_create_parent_directory(self):
        path = self.root / "absent" / "new.json"
        for option in (0, 1, None, "true", {}, []):
            with self.assertRaises(ValueError):
                save(path, {}, compact=option)
            self.assertFalse(path.parent.exists())
        self.unchanged()

    def test_compact_encoding_failure_retains_previous_bytes(self):
        with self.assertRaises(UnicodeEncodeError):
            save(self.path, {"text": "\ud800"}, compact=True)
        self.unchanged()

    def test_explicit_compact_keeps_existing_default_string_conversion(self):
        value = {"path": self.root, "values": [0, -0.0, 1e-20, False]}
        save(self.path, value, compact=True)
        self.assertEqual(json.loads(self.path.read_bytes()), json.loads(json.dumps(value, default=str)))
        self.assertEqual(self.path.read_bytes()[-1:], b"\n")
