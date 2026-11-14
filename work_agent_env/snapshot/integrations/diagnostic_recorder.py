"""Durable partial evidence for scripted local diagnostics, without replay."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from integrations.nemo_http import NeMoHTTP
from workenv.core import no_links, save
from workenv.handoff import validate_member

MAX_EVENTS = 5000
MAX_EVENT_BYTES = 256 * 1024
MAX_JOURNAL_BYTES = 8 * 1024 * 1024


class DiagnosticRecorder:
    def __init__(self, root, directory, expected_tasks, source_sha256):
        self.root, self.directory = Path(root).resolve(), Path(directory).absolute()
        no_links(self.directory, self.root)
        if (not 1 <= len(expected_tasks) <= 128 or len(set(expected_tasks)) != len(expected_tasks)
            or any(not isinstance(t, str) or not t or len(t) > 200 for t in expected_tasks)):
            raise ValueError("Expected diagnostic task inventory is invalid")
        if not source_sha256 or len(source_sha256) > 128: raise ValueError("Missing bounded diagnostic source identity")
        for name, checksum in source_sha256.items():
            validate_member(name)
            if not isinstance(checksum, str) or not re.fullmatch("[0-9a-f]{64}", checksum):
                raise ValueError("Invalid diagnostic source hash")
        self.expected_tasks, self.completed_tasks = list(expected_tasks), []
        self.source_sha256 = dict(source_sha256)
        self.in_flight, self.events, self.bytes = None, 0, 0
        self.checksum = hashlib.sha256()
        self.journal_path = self.directory / "http_journal.partial.jsonl"
        no_links(self.journal_path, self.root)
        # Exclusive creation forbids resuming a previous process's uncertain writes.
        with self.journal_path.open("xb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        self._progress("running")

    def _progress(self, state, error_type=None):
        save(self.directory / "progress.json", {
            "schema_version": "scripted-diagnostic-progress-v1", "at": datetime.now(timezone.utc).isoformat(),
            "execution_kind": "scripted_diagnostic", "state": state,
            "passed": state == "passed", "owner_pid": os.getpid(), "process_liveness_attested": False,
            "expected_tasks": self.expected_tasks, "completed_tasks": list(self.completed_tasks),
            "in_flight_task": self.in_flight, "event_count": self.events, "journal_bytes": self.bytes,
            "journal_ref": self.journal_path.name, "journal_prefix_sha256": self.checksum.hexdigest(),
            "error_type": error_type, "automatic_resume": False, "training_eligible": False,
            "real_model_calls": 0, "training_updates": 0, "source_sha256": self.source_sha256})

    def start_task(self, task):
        if self.in_flight is not None or task not in self.expected_tasks or task in self.completed_tasks:
            raise ValueError("Diagnostic task is duplicate, unknown or already in flight")
        self.in_flight = task
        self._progress("running")

    def complete_task(self, task):
        if self.in_flight != task: raise ValueError("Diagnostic completion does not match its active task")
        self.completed_tasks.append(task)
        self.in_flight = None
        self._progress("running")

    def record(self, event):
        raw = (json.dumps({**event, "event_index": self.events}, allow_nan=False, separators=(",", ":")) + "\n").encode()
        if len(raw) > MAX_EVENT_BYTES or self.events >= MAX_EVENTS or self.bytes + len(raw) > MAX_JOURNAL_BYTES:
            self._progress("failed", "EvidenceBudgetExceeded")
            raise ValueError("Partial diagnostic journal exceeds its evidence budget")
        no_links(self.journal_path, self.root)
        with self.journal_path.open("ab") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        self.checksum.update(raw)
        self.bytes += len(raw)
        self.events += 1
        self._progress("running")

    def finish(self, passed, error_type=None):
        complete = self.in_flight is None and set(self.completed_tasks) == set(self.expected_tasks)
        if passed and not complete: raise ValueError("Partial diagnostic execution cannot be marked passed")
        self._progress("passed" if passed else "failed", error_type)


class RecordedNeMoHTTP(NeMoHTTP):
    def __init__(self, recorder):
        super().__init__()
        self.recorder = recorder

    def post(self, route, body, *, expected=200):
        previous = len(self.journal)
        try:
            result = super().post(route, body, expected=expected)
        except Exception as exc:
            event = self.journal[-1] if len(self.journal) > previous else {
                "route": route, "request": body, "status": None,
                "transport_error_type": type(exc).__name__, "operation_outcome": "unknown_no_automatic_retry"}
            self.recorder.record(event)
            raise
        self.recorder.record(self.journal[-1])
        return result
