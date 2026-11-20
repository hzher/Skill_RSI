"""Transparent Messages transport with an ingress ledger and durable raw streams."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request

from .storage import atomic
from .wire import StreamTiming, decode_response


def native_contract(request):
    system = request.get("system", "")
    if isinstance(system, list):
        system = "\n".join(block.get("text", "") for block in system)
    names = {tool.get("name") for tool in request.get("tools", [])}
    identity = "You are Claude Code" in system or "You are a Claude agent, built on Anthropic's Claude Agent SDK." in system
    full_prompt = identity and all(heading in system for heading in ("# Harness", "# Environment", "# Context management"))
    task_tools = {"mcp__benchmark__act", "mcp__benchmark__status"} <= names
    return {"accepted": full_prompt and task_tools and "Skill" not in names,
            "full_claude_code_prompt": full_prompt, "task_tools_present": task_tools,
            "actual_tools": sorted(names)}


class RecordingBridge:
    def __init__(self, base_url, key, model, path, *, bind="127.0.0.1", timeout=900):
        self.path = Path(path)
        self.model = model
        self.key = key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.counter = 0
        self.attempts = {}
        self.attempt_counts = {}
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                started = time.monotonic()
                with bridge.lock:
                    index = bridge.counter
                    bridge.counter += 1
                path = bridge.path / f"{index:04d}"
                path.mkdir(parents=True)
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                (path / "request.raw").write_bytes(raw)
                identity = hashlib.sha256(raw).hexdigest()
                atomic(path / "intent.json", {"time": time.time(), "route": urlsplit(self.path).path,
                       "request_sha256": identity, "request_bytes": len(raw)})
                incoming = None
                status = 400
                forwarded = False
                response_started = False
                response = None
                timing = StreamTiming(started)
                headers_seconds = None
                failure = None
                body = b""
                try:
                    incoming = json.loads(raw)
                    atomic(path / "request.json", incoming)
                    if urlsplit(self.path).path != "/" + bridge.token + "/v1/messages":
                        status = 404
                        raise ValueError("Unrecognized Messages route")
                    if incoming.get("model") != bridge.model:
                        raise ValueError("Unexpected requested model")
                    contract = native_contract(incoming)
                    atomic(path / "native-contract.json", contract)
                    if not contract["accepted"]:
                        raise ValueError("Native prompt or benchmark tools differ from the declared contract")
                    with bridge.lock:
                        previous = bridge.attempts.get(identity)
                        if previous and (previous != "retryable" or bridge.attempt_counts[identity] >= 3):
                            status = 409
                            raise ValueError("Prior identical request needs delivery reconciliation")
                        bridge.attempts[identity] = "pending"
                        bridge.attempt_counts[identity] = bridge.attempt_counts.get(identity, 0) + 1
                    headers = {"Content-Type": "application/json", "x-api-key": bridge.key,
                               "Authorization": "Bearer " + bridge.key,
                               "anthropic-version": self.headers.get("anthropic-version", "2023-06-01")}
                    if self.headers.get("anthropic-beta"):
                        headers["anthropic-beta"] = self.headers["anthropic-beta"]
                    query = urlsplit(self.path).query
                    url = bridge.base_url + "/messages" + ("?" + query if query else "")
                    forwarded = True
                    status = 0
                    atomic(path / "forward.json", {"time": time.time(), "url": url, "request_sha256": identity})
                    request = urllib.request.Request(url, data=raw, headers=headers)
                    try:
                        response = urllib.request.urlopen(request, timeout=bridge.timeout)
                    except urllib.error.HTTPError as error:
                        response = error
                    status = response.status if hasattr(response, "status") else response.code
                    headers_seconds = time.monotonic() - started
                    public = {key: value for key, value in response.headers.items() if key.lower() in (
                        "content-type", "request-id", "x-request-id", "retry-after", "server", "via",
                        "cf-ray", "x-model", "x-upstream-model", "anthropic-organization-id", "anthropic-workspace-id")}
                    atomic(path / "headers.json", {"status": status, "headers": public})
                    self.send_response(status)
                    self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                    if response.headers.get("Retry-After"):
                        self.send_header("Retry-After", response.headers["Retry-After"])
                    self.end_headers()
                    response_started = True
                    chunks = []
                    client_gone = False
                    with (path / "response.raw").open("wb") as output, (path / "chunk-arrivals.jsonl").open("w", buffering=1) as arrivals:
                        while True:
                            chunk = response.read1(65536)
                            if not chunk:
                                break
                            timing.feed(chunk, time.monotonic())
                            arrivals.write(json.dumps(timing.chunks[-1]) + "\n")
                            chunks.append(chunk)
                            output.write(chunk)
                            output.flush()
                            if not client_gone:
                                try:
                                    self.wfile.write(chunk)
                                    self.wfile.flush()
                                except (BrokenPipeError, ConnectionResetError):
                                    client_gone = True
                            if time.monotonic() - started > bridge.timeout:
                                raise TimeoutError("Response recording deadline reached")
                    body = b"".join(chunks)
                    atomic(path / "delivery.json", {"client_disconnected": client_gone})
                except Exception as error:
                    failure = {"kind": type(error).__name__, "message": str(error)}
                    atomic(path / "failure.json", failure)
                    if (path / "response.raw").exists():
                        body = (path / "response.raw").read_bytes()
                    if not response_started:
                        try:
                            self.send_error(status if not forwarded else 502, "Inspect retained request receipt")
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                finally:
                    if response is not None:
                        response.close()
                    parsed = decode_response(body, status)
                    if not forwarded:
                        parsed.update(complete=False, disposition="local_rejection")
                    total = time.monotonic() - started
                    usage = parsed.get("final_usage") or {}
                    output_tokens = usage.get("output_tokens") if parsed["complete"] else None
                    atomic(path / "receipt.json", {**parsed, "http_status": status or None, "forwarded": forwarded,
                           "seconds": total, "bytes": len(body), "failure": failure})
                    atomic(path / "timing.json", timing.summary(output_tokens, total, headers_seconds))
                    atomic(path / "stream-chunks.json", timing.chunks)
                    atomic(path / "stream-events.json", timing.events)
                    if forwarded:
                        with bridge.lock:
                            bridge.attempts[identity] = "retryable" if status in (429, 502, 503, 504) or parsed["explicit_upstream_error"] else parsed["disposition"]

        self.server = ThreadingHTTPServer((bind, 0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.key = ""
