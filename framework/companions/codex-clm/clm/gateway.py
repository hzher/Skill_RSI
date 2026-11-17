"""Stream native Codex Responses over a session-local Unix socket."""

import http.client
import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

from .context import Context, encode

MODEL = "gpt-6.1-sol"
LOCAL_KEY = "codex-clm-local-session"
MAX_REQUEST = 16 * 1024 * 1024


class Gateway(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, state, audit, base_url, key, marker, max_requests=None):
        self.endpoint = urlsplit(base_url)
        if (
            self.endpoint.scheme != "https" or not self.endpoint.hostname
            or self.endpoint.username or self.endpoint.password
            or self.endpoint.query or self.endpoint.fragment
        ):
            raise ValueError("OPENAI_BASE_URL must be an HTTPS API base URL")
        self.context = Context(state)
        self.audit = audit
        self.key = key
        self.marker = marker
        self.max_requests = max_requests
        self.requests = 0
        self.lock = threading.Lock()
        super().__init__(str(path), Handler)

    def record(self, event):
        with self.lock, self.audit.open("ab") as stream:
            stream.write(encode(event) + b"\n")


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(180)
        super().setup()

    def log_message(self, *_):
        pass

    def error_json(self, status, message):
        payload = encode({"error": {"message": message, "type": "invalid_request_error"}})
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        try:
            self.forward()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def forward(self):
        if self.path != "/v1/responses":
            self.error_json(404, "This provider accepts POST /v1/responses")
            return
        if self.headers.get("Authorization") != f"Bearer {LOCAL_KEY}":
            self.error_json(401, "Missing local session credential")
            return
        server = self.server
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST:
                raise ValueError("request body size out of bounds")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or payload.get("model") != MODEL:
                raise ValueError(f"expected model {MODEL}")
            if payload.get("previous_response_id") or payload.get("conversation"):
                raise ValueError("CLM requires full stateless Responses input")
            instructions = payload.get("instructions") or ""
            developer_items = [
                item for item in payload.get("input", [])
                if isinstance(item, dict) and item.get("role") == "developer"
            ]
            if server.marker not in instructions and server.marker.encode() not in encode(developer_items):
                raise ValueError("request lacks CLM session instructions")
            if payload.get("store") is not False:
                raise ValueError("CLM requires store:false")
            with server.lock:
                if server.max_requests is not None and server.requests >= server.max_requests:
                    self.error_json(429, "CLM run request limit reached")
                    return
                payload["input"], event = server.context.apply(payload["input"])
                server.requests += 1
        except (KeyError, TypeError, OSError, ValueError) as error:
            # Avoid exposing handler exceptions, user content or credentials in logs.
            server.record({"route": self.path, "http_status": 400, "validation": type(error).__name__})
            self.error_json(400, str(error) if type(error) is ValueError else "Invalid Responses input")
            return

        endpoint = server.endpoint
        base = endpoint.path.rstrip("/")
        target = (base if base.endswith("/v1") else base + "/v1") + "/responses"
        headers = {
            "Authorization": f"Bearer {server.key}",
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
            "Accept": self.headers.get("Accept", "text/event-stream"),
        }
        for name in ("OpenAI-Beta", "User-Agent"):
            if value := self.headers.get(name):
                headers[name] = value
        conn = http.client.HTTPSConnection(endpoint.hostname, endpoint.port, timeout=150)
        event.update(route=self.path, model=MODEL, stream=bool(payload.get("stream")))
        started = False
        try:
            conn.request("POST", target, body=encode(payload), headers=headers)
            response = conn.getresponse()
            event["http_status"] = response.status
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in ("content-type", "content-encoding", "retry-after", "x-request-id"):
                    self.send_header(key, value)
            self.end_headers()
            started = True
            received = 0
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                received += len(chunk)
            event.update(transport="eof", response_bytes=received)
        except (OSError, http.client.HTTPException) as error:
            event["transport"] = type(error).__name__
            if not started:
                self.error_json(502, "Provider request failed")
        finally:
            conn.close()
            server.record(event)
