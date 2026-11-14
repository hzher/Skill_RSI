"""Real Windows sharing handles plus error-path invariants for atomic writes."""
import errno
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts.probe_atomic_replace import deny_delete
from workenv.atomic_io import _retryable,replace_atomic
from workenv.core import read_json,save


class AtomicIOTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory=Path(temporary.name)
        self.path=self.directory/"record.json"
        save(self.path,{"version":1})

    @unittest.skipUnless(os.name=="nt","Windows sharing handles")
    def test_transient_real_deny_delete_handle_recovers_without_partial_json(self):
        start=time.monotonic()
        with deny_delete(self.path):save(self.path,{"version":2})
        self.assertGreaterEqual(time.monotonic()-start,.08)
        self.assertLess(time.monotonic()-start,2)
        self.assertEqual(read_json(self.path),{"version":2})
        self.assertEqual(list(self.directory.glob("*.tmp")),[])

    @unittest.skipUnless(os.name=="nt","Windows sharing handles")
    def test_persistent_real_handle_still_fails_and_preserves_original(self):
        start=time.monotonic()
        with deny_delete(self.path,None):
            with self.assertRaises(PermissionError):save(self.path,{"version":2})
            self.assertEqual(read_json(self.path),{"version":1})
            self.assertEqual(list(self.directory.glob("*.tmp")),[])
        self.assertLess(time.monotonic()-start,2)

    def test_unrelated_os_error_is_not_retried_and_original_remains(self):
        error=OSError(errno.ENOSPC,"explicit fixture no space")
        with patch.object(Path,"replace",side_effect=error) as replacement:
            with self.assertRaises(OSError) as raised:save(self.path,{"version":2})
            self.assertIs(raised.exception,error)
            self.assertEqual(replacement.call_count,1)
        self.assertEqual(read_json(self.path),{"version":1})
        self.assertEqual(list(self.directory.glob("*.tmp")),[])

    def test_retry_classification_is_limited_to_windows_permission_codes(self):
        for code in (5,32,33,123,None):
            error=PermissionError("explicit fixture")
            error.winerror=code
            self.assertEqual(_retryable(error),os.name=="nt" and code in (5,32,33))
        self.assertFalse(_retryable(OSError(errno.EIO,"fixture")))

    def test_invalid_retry_budgets_do_not_touch_the_original(self):
        source=self.directory/"replacement"
        source.write_bytes(b"new")
        for budget in (-1,2,True,None,float("nan"),float("inf")):
            with self.assertRaises(ValueError):replace_atomic(source,self.path,retry_seconds=budget)
        self.assertEqual(source.read_bytes(),b"new")
        self.assertEqual(read_json(self.path),{"version":1})

    def test_serialization_failure_leaves_original_and_no_temporary(self):
        circular=[]
        circular.append(circular)
        with self.assertRaises(ValueError):save(self.path,circular)
        self.assertEqual(read_json(self.path),{"version":1})
        self.assertEqual(list(self.directory.glob("*.tmp")),[])
