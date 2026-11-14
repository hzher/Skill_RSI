"""Private resource IPC with bounded frames and no retry of uncertain operations."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid

from integrations.episode_cluster import EpisodeProfile
from integrations.resource_transport import MAX_REQUEST_FRAME_BYTES, MAX_RESPONSE_FRAME_BYTES, encode_frame
from workenv.core import ResourceError, no_links, read_json
from workenv.process_identity import process_identity


class EpisodeWorkerClient:
    def __init__(self, root, *, slot, seed_sql, timeout=180, resource_id=None, on_spawn=None, task_id="wc-sales-tax-summary"):
        if task_id not in ("wc-sales-tax-summary", "howtocook-meal-plan-gcal", "wc-order-analysis-gcal", "wc-order-fulfillment-tracker", "wc-shipping-zone-gsheet", "wc-customer-order-gsheet-email"):
            raise ValueError("Unsupported isolated worker task")
        if type(timeout) not in (int, float) or not 1 <= timeout <= 300:
            raise ValueError("Private resource worker timeout must be 1..300 seconds")
        self.root, self.timeout = Path(root).resolve(), timeout
        self.profile = EpisodeProfile(self.root, resource_id or str(uuid.uuid4()), slot)
        self.lock, self.inbox = threading.Lock(), queue.Queue(maxsize=2)
        self.poisoned, self.frames = False, 0
        logdir = self.root / ".runtime/native-episodes/worker-logs"
        no_links(logdir, self.root)
        logdir.mkdir(parents=True, exist_ok=True)
        self.stderr = (logdir / (self.profile.resource_id + ".stderr.txt")).open("xb")
        self.process = subprocess.Popen([sys.executable, "-u", "-m", "integrations.episode_worker", "--slot", str(slot),
            "--resource-id", self.profile.resource_id, "--seed-sql", str(seed_sql), "--task-id", task_id], cwd=self.root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if on_spawn is not None: on_spawn(self)
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            ready = self._receive()
            marker = self.profile.base / "cluster_owner.json"
            lease = self.profile.base.parent / ("slot-" + str(slot) + ".lock")
            no_links(marker, self.root)
            no_links(lease, self.root)
            if (ready.get("kind") != "isolated-resource-worker-ready" or ready.get("profile") != self.profile.record()
                or ready.get("process_identity") != process_identity(ready["pid"]) or ready["process_identity"]["status"] != "alive"
                or read_json(marker) != self.profile.record() or read_json(lease).get("process") != ready["process_identity"]):
                raise ResourceError("Isolated resource worker ownership was not confirmed")
            if task_id != "wc-sales-tax-summary" and ready.get("task_id") != task_id:
                raise ResourceError("Isolated worker selected another task")
            self.ready = ready
        except Exception:
            self.stop()
            raise

    def _reader(self):
        try:
            while True:
                raw = self.process.stdout.readline(MAX_RESPONSE_FRAME_BYTES + 1)
                if not raw: break
                if len(raw) > MAX_RESPONSE_FRAME_BYTES or not raw.endswith(b"\n"):
                    raise ResourceError("Isolated worker response frame exceeds its budget")
                self.inbox.put(json.loads(raw))
        except Exception as exc:
            self.inbox.put(exc)
        finally:
            self.inbox.put(None)

    def _receive(self):
        try: value = self.inbox.get(timeout=self.timeout)
        except queue.Empty as exc:
            self.poisoned = True
            raise ResourceError("Isolated worker timed out; outcome unknown, do not replay") from exc
        if value is None or isinstance(value, Exception):
            self.poisoned = True
            raise ResourceError("Isolated worker disconnected; outcome unknown")
        return value

    def call(self, route, body):
        with self.lock:
            if self.poisoned or self.frames >= 128:
                raise ResourceError("Isolated worker is unavailable or frame budget exhausted")
            identifier = str(uuid.uuid4())
            raw = encode_frame({"id": identifier, "route": route, "body": body}, MAX_REQUEST_FRAME_BYTES)
            self.frames += 1
            try:
                self.process.stdin.write(raw)
                self.process.stdin.flush()
            except OSError as exc:
                self.poisoned = True
                raise ResourceError("Worker transport failed; operation outcome unknown") from exc
            result = self._receive()
            if result.get("id") != identifier:
                self.poisoned = True
                raise ResourceError("Isolated worker response identity mismatch")
            return result

    def stop(self):
        if self.process.stdin and not self.process.stdin.closed: self.process.stdin.close()
        try:
            self.process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            self.poisoned = True
            raise ResourceError("Worker cleanup has not completed; retain its slot for recovery") from exc
        finally:
            if self.process.poll() is not None:
                self.process.stdout.close()
                self.stderr.close()
