"""Critical path and evidence-redaction guards, independent of the task fixtures."""
import os
import subprocess
import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from workenv.core import ROOT, run_directory
from workenv import core
from record_command import redact


class LifecycleSafetyTests(unittest.TestCase):
    def test_linked_run_root_is_rejected_before_any_run_write(self):
        with tempfile.TemporaryDirectory(prefix="workenv-path-test-") as temp:
            scratch=Path(temp).resolve()
            owned=scratch/"owned"
            target=scratch/"redirected"
            owned.mkdir();target.mkdir()
            link=owned/"runs"
            if os.name=="nt":
                result=subprocess.run(["cmd","/c","mklink","/J",str(link),str(target)],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode,0)
            else:link.symlink_to(target,target_is_directory=True)
            try:
                with patch.object(core,"ROOT",owned):
                    with self.assertRaises(ValueError):run_directory("probe")
                try:
                    from adapters import toolathlon_native
                except ImportError:toolathlon_native=None
                if toolathlon_native is not None:
                    with patch.object(toolathlon_native,"ROOT",owned):
                        with self.assertRaises(ValueError):toolathlon_native.run_directory("probe")
                self.assertEqual(list(target.iterdir()),[])
            finally:
                self.assertTrue(link.absolute().is_relative_to(scratch))
                self.assertTrue(target.resolve().is_relative_to(scratch))
                if link.is_symlink():link.unlink()
                else:link.rmdir()

    def test_native_path_guards_when_runtime_dependencies_are_installed(self):
        try:
            from adapters.toolathlon_native import run_directory as native_directory
        except ImportError:
            self.skipTest("Optional PostgreSQL/Excel runtime dependencies are not installed")
        for value in ("..","../elsewhere","..\\elsewhere","E:\\other","/tmp/other","run/child"):
            with self.subTest(value=value),self.assertRaises(ValueError):native_directory(value)
        self.assertEqual(native_directory("valid_run_123"),run_directory("valid_run_123"))

    def test_run_ids_cannot_escape_owned_directory(self):
        for value in ("..", "../elsewhere", "..\\elsewhere", "E:\\other", "/tmp/other", "run/child"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                run_directory(value)

    def test_valid_run_stays_inside_runs(self):
        path = run_directory("valid_run_123")
        self.assertTrue(path.is_relative_to((ROOT / "runs").resolve()))

    def test_evidence_redacts_secret_environment_value(self):
        fake = "test-only-private-value-12345"
        with patch.dict(os.environ, {"MODEL_API_KEY": fake}):
            result = redact("request failed using " + fake)
        self.assertNotIn(fake, result)
        self.assertIn("[REDACTED:MODEL_API_KEY]", result)

    def test_embedded_url_credentials_are_redacted(self):
        self.assertEqual(redact("https://user:fakepassword@example.com/path"), "https://[REDACTED]@example.com/path")


if __name__ == "__main__":
    unittest.main()
