"""Session-scoped Messages proxy. Provider credentials stay on the DSW host."""

import http.client
import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

from .context import Context, encode

MAX_REQUEST = 16 * 1024 * 1024
LOCAL_KEY = "clm-session-local"
ROUTES = {"/v1/messages", "/v1/messages/count_tokens"}


class Gateway(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path, state, audit, base_url, api_key, model, marker):
        self.endpoint = urlsplit(base_url)
        if (
            self.endpoint.scheme != "https"
            or not self.endpoint.hostname
            or self.endpoint.username
            or self.endpoint.password
            or self.endpoint.query
            or self.endpoint.fragment
        ):
            raise ValueError("ANTHROPIC_BASE_URL must be an HTTPS base URL")
        self.context = Context(state)
        self.audit = audit
        self.key = api_key
        self.model = model
        self.marker = marker
        self.lock = threading.Lock()
        super().__init__(str(socket_path), Handler)

    def record(self, record):
        with self.lock, self.audit.open("ab") as stream:
            stream.write(encode(record) + b"\n")


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(180)
        super().setup()

    def log_message(self, *_):
        pass

    def error_json(self, status, message):
        payload = encode({"type": "error", "error": {"type": "invalid_request_error", "message": message}})
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
        route = urlsplit(self.path)
        if route.path not in ROUTES or route.query not in ("", "beta=true"):
            self.error_json(404, "Only Messages and count_tokens are available")
            return
        if self.headers.get("x-api-key") != LOCAL_KEY and self.headers.get(
            "Authorization"
        ) != "Bearer " + LOCAL_KEY:
            self.error_json(401, "Missing local session credential")
            return
        server = self.server
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
            if payload.get("model") != server.model:
                self.error_json(400, "Request model differs from the session model")
                return
            if server.marker not in encode(payload.get("system")).decode():
                self.error_json(400, "Request lacks the CLM session system prompt")
                return
            messages = payload["messages"]
            if not isinstance(messages, list):
                raise ValueError
            with server.lock:
                payload["messages"], event = server.context.apply(
                    messages, accept_edit=route.path == "/v1/messages"
                )
        except (TypeError, ValueError, KeyError, OSError, AttributeError):
            self.error_json(400, "Invalid Messages request or unavailable context")
            return

        endpoint = server.endpoint
        target = endpoint.path.rstrip("/") + route.path
        if route.query:
            target += "?" + route.query
        headers = {
            key: value for key, value in self.headers.items()
            if key.lower().startswith("anthropic-")
        }
        headers.update({
            "content-type": "application/json",
            "accept-encoding": "identity",
            "x-api-key": server.key,
            "authorization": "Bearer " + server.key,
        })
        headers.setdefault("anthropic-version", "2023-06-01")
        connection = http.client.HTTPSConnection(endpoint.hostname, endpoint.port, timeout=150)
        event.update(route=route.path, stream=bool(payload.get("stream")))
        response_started = False
        try:
            connection.request("POST", target, body=encode(payload), headers=headers)
            response = connection.getresponse()
            event["http_status"] = response.status
            self.send_response(response.status)
            for key, value in response.getheaders():
                name = key.lower()
                if name in ("content-type", "content-encoding", "retry-after", "request-id", "x-request-id"):
                    self.send_header(key, value)
            self.end_headers()
            response_started = True
            received = 0
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                received += len(chunk)
            event["response_bytes"] = received
            event["transport"] = "eof"
        except (OSError, http.client.HTTPException) as error:
            event["transport"] = type(error).__name__
            if not response_started:
                self.error_json(502, "Provider request failed")
        finally:
            connection.close()
            server.record(event)
