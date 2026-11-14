import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from integrations.diagnostic_recorder import DiagnosticRecorder, RecordedNeMoHTTP
from integrations.synthetic_audit import audit_incremental


class DiagnosticRecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "diagnostic"
        self.output.mkdir()

    def recorder(self):
        return DiagnosticRecorder(self.root, self.output, ["unit-one", "unit-two"], {"scripts/unit.py": "a" * 64})

    def test_partial_run_cannot_pass_or_reopen_an_existing_journal(self):
        r = self.recorder()
        r.start_task("unit-one")
        r.record({"route": "/unit", "status": 200, "response": {"fixture": True}})
        r.complete_task("unit-one")
        with self.assertRaises(ValueError): r.finish(True)
        with self.assertRaises(ValueError): r.start_task("unit-one")
        with self.assertRaises(FileExistsError): self.recorder()
        r.finish(False, "UnitIncomplete")
        progress = json.loads((self.output / "progress.json").read_text())
        self.assertEqual(progress["completed_tasks"], ["unit-one"])
        self.assertEqual(progress["state"], "failed")
        self.assertFalse(progress["automatic_resume"])

    def test_completed_progress_binds_exact_journal_bytes_and_all_tasks(self):
        r = self.recorder()
        for task in ("unit-one", "unit-two"):
            r.start_task(task)
            r.record({"route": "/unit", "status": 200})
            r.complete_task(task)
        r.finish(True)
        progress = json.loads((self.output / "progress.json").read_text())
        self.assertTrue(progress["passed"])
        self.assertEqual(progress["event_count"], 2)
        self.assertEqual(progress["journal_prefix_sha256"], hashlib.sha256((self.output / "http_journal.partial.jsonl").read_bytes()).hexdigest())
        receipt = {"progress_sha256": hashlib.sha256((self.output / "progress.json").read_bytes()).hexdigest(),
                   "incremental_journal_sha256": progress["journal_prefix_sha256"], "source_sha256": r.source_sha256}
        events = [{"route": "/unit", "status": 200}] * 2
        self.assertEqual(audit_incremental(self.root, self.output / "result.json", receipt, events, ["unit-one", "unit-two"])["event_count"], 2)
        with self.assertRaises(ValueError):
            audit_incremental(self.root, self.output / "result.json", receipt, events[:1], ["unit-one", "unit-two"])
        (self.output / "http_journal.partial.jsonl").write_bytes(b'changed')
        with self.assertRaises(ValueError):
            audit_incremental(self.root, self.output / "result.json", receipt, events, ["unit-one", "unit-two"])

    def test_transport_failure_is_unknown_and_is_not_retried(self):
        r = self.recorder()
        client = RecordedNeMoHTTP(r)
        class BrokenTransport:
            calls = 0
            def open(self, *args, **kwargs):
                self.calls += 1
                raise TimeoutError("unit transport failure")
        transport = BrokenTransport()
        client.opener = transport
        with self.assertRaises(TimeoutError): client.post("/unit", {"fixture": True})
        event = json.loads((self.output / "http_journal.partial.jsonl").read_text())
        self.assertEqual(event["operation_outcome"], "unknown_no_automatic_retry")
        self.assertIsNone(event["status"])
        self.assertEqual(transport.calls, 1)

    def test_owned_child_termination_preserves_last_durable_progress(self):
        code = '''import sys
from pathlib import Path
from integrations.diagnostic_recorder import DiagnosticRecorder
r=DiagnosticRecorder(Path(sys.argv[1]),Path(sys.argv[2]),["unit-one","unit-two"],{"scripts/unit.py":"a"*64})
r.start_task("unit-one")
r.record({"route":"/unit","status":200,"execution_kind":"unit_fixture"})
r.complete_task("unit-one")
r.start_task("unit-two")
print("durable",flush=True)
sys.stdin.read()
'''
        proc = subprocess.Popen([sys.executable, "-c", code, str(self.root), str(self.output)],
                                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        try:
            self.assertEqual(proc.stdout.readline().strip(), b"durable")
            proc.terminate()
            proc.wait(timeout=5)
            progress = json.loads((self.output / "progress.json").read_text())
            self.assertEqual(progress["completed_tasks"], ["unit-one"])
            self.assertEqual(progress["in_flight_task"], "unit-two")
            self.assertFalse(progress["passed"])
            self.assertFalse(progress["process_liveness_attested"])
            self.assertTrue((self.output / "http_journal.partial.jsonl").read_bytes().endswith(b"\n"))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr): stream.close()
