"""Linux owner identity and PostgreSQL binary lookup contracts."""
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import setup_linux
from workenv.postgres_binary import postgres_binary
from workenv.process_identity import process_identity
from integrations.episode_cluster import probe_endpoint


@unittest.skipUnless(sys.platform == "linux", "Linux process ownership contract")
class LinuxHostPortabilityTests(unittest.TestCase):
    def test_endpoint_probe_refuses_a_live_foreign_listener(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with self.assertRaises(OSError): probe_endpoint(*listener.getsockname())
            self.assertGreater(listener.fileno(), 0)

    def test_endpoint_probe_allows_time_wait_after_an_owned_connection_closes(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            address = listener.getsockname()
            listener.listen(1)
            with socket.create_connection(address, timeout=2) as client:
                connection, _ = listener.accept()
                connection.close()  # Server sends FIN first and owns TIME_WAIT.
                self.assertEqual(client.recv(1), b"")
        with socket.socket() as plain_probe:
            with self.assertRaises(OSError): plain_probe.bind(address)
        probe_endpoint(*address)

    def test_pinned_source_checkout_resumes_after_git_init(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote"
            remote.mkdir()
            subprocess.run(["git", "init", "-q", str(remote)], check=True)
            subprocess.run(["git", "-C", str(remote), "-c", "user.name=Test",
                            "-c", "user.email=test@example.invalid", "commit", "-q", "--allow-empty", "-m", "pin"], check=True)
            pin = subprocess.check_output(["git", "-C", str(remote), "rev-parse", "HEAD"], text=True).strip()
            target = root / ".upstream/test"
            target.parent.mkdir()
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            subprocess.run(["git", "-C", str(target), "remote", "add", "origin", str(remote)], check=True)
            with patch.object(setup_linux, "ROOT", root), patch.dict(setup_linux.LOCK, {"fixture": {
                "checkout": ".upstream/test", "url": str(remote), "commit": pin}}):
                setup_linux.checkout("fixture", sparse=True)
                setup_linux.checkout("fixture", sparse=True)
            self.assertEqual(subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"],
                                                     text=True).strip(), pin)

    def test_child_identity_is_stable_and_exit_is_confirmed(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            first = process_identity(child.pid)
            second = process_identity(child.pid)
            self.assertEqual(first, second)
            self.assertEqual(first["status"], "alive")
            self.assertTrue(first["creation_id"].startswith("linux-proc:"))
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertIn(process_identity(child.pid)["status"], ("not_found", "exited"))

    def test_unreadable_boot_identity_is_unknown_not_exit_proof(self):
        original = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == "/proc/sys/kernel/random/boot_id":
                raise FileNotFoundError("unavailable boot identity")
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", read):
            observed = process_identity(os.getpid())
        self.assertEqual(observed["status"], "unknown")
        self.assertIsNone(observed["creation_id"])

    def test_explicit_postgres_binary_directory_and_unknown_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "pg_ctl"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            binary.chmod(0o700)
            with patch.dict(os.environ, {"WORKENV_PG_BIN": directory}):
                self.assertEqual(postgres_binary(root, "pg_ctl"), binary)
            with self.assertRaises(ValueError):
                postgres_binary(root, "not-a-postgres-tool")

    def test_user_postgres_stage_reuses_complete_version_15_toolset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary_dir = root / "pgsql/bin"
            binary_dir.mkdir(parents=True)
            for name in setup_linux.PG_TOOLS:
                binary = binary_dir / name
                binary.write_text(f"#!/bin/sh\necho '{name} (PostgreSQL) 15.19'\n", encoding="ascii")
                binary.chmod(0o700)
            with patch.object(setup_linux, "ROOT", root), patch.dict(os.environ, {"WORKENV_PG_BIN": str(binary_dir)}):
                setup_linux.postgresql()
            self.assertFalse((root / ".runtime").exists())


if __name__ == "__main__": unittest.main()
