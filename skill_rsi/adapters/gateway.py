"""Optional loopback HTTP recorder for explicitly routed Messages and Responses."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from ..evidence.archive import pointer, write
from ..evidence.io import digest, write_bytes
from ..evidence.schema import NativeRun, event, identifier
from .native_events import mapping
from .wire import decode_response

_MAX_REQUEST = 32 * 1024 * 1024
_HEADERS = ("Content-Type", "Authorization", "x-api-key", "OpenAI-Organization", "OpenAI-Project",
            "OpenAI-Beta", "anthropic-version", "anthropic-beta")
_ROUTES = {"claude_code": "messages", "codex": "responses"}


def _stamp():
    return datetime.now(timezone.utc).isoformat()


class WireRecorder:
    """Preserve the client's wire body and the bytes actually received from upstream."""

    def __init__(self, host, upstream, output, *, credential=None, port=0, timeout=120):
        parsed = urlsplit(upstream)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Expected an explicit upstream HTTP or HTTPS URL without credentials or query")
        if host not in _ROUTES:
            raise ValueError(f"Unsupported recorder host {host!r}")
        self.host = host
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.token = secrets.token_urlsafe(24)
        route = "/" + self.token + "/" + _ROUTES[host]
        connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):
                request_url = urlsplit(self.path)
                if request_url.path not in {route, "/" + recorder.token + "/v1/" + _ROUTES[host]}:
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= _MAX_REQUEST:
                        raise ValueError
                    raw = self.rfile.read(length)
                    if len(raw) != length or not isinstance(json.loads(raw), dict):
                        raise ValueError
                except (ValueError, UnicodeError):
                    self.send_error(400)
                    return
                key = uuid4().hex
                directory = recorder.output / key
                directory.mkdir(mode=0o700)
                write_bytes(directory / "request.raw", raw)
                write(directory / "request.json", json.loads(raw))
                upstream_path = parsed.path.rstrip("/") + "/" + _ROUTES[host]
                if request_url.query:
                    upstream_path += "?" + request_url.query
                write(directory / "intent.json", {"time": _stamp(), "request_sha256": digest(raw),
                                                   "request_bytes": len(raw), "route": _ROUTES[host]})
                headers = {name: self.headers[name] for name in _HEADERS if name in self.headers}
                if credential is not None:
                    if host == "codex":
                        headers["Authorization"] = "Bearer " + credential
                    else:
                        headers["x-api-key"] = credential
                        headers.pop("Authorization", None)
                connection = connection_class(parsed.hostname, parsed.port, timeout=timeout)
                response_started = False
                forwarded = False
                client_disconnected = False
                received, status, complete, failure = 0, None, False, None
                try:
                    connection.request("POST", upstream_path, body=raw, headers=headers)
                    forwarded = True
                    write(directory / "forward.json", {"time": _stamp(), "request_sha256": digest(raw),
                                                        "socket_write_observed": True})
                    response = connection.getresponse()
                    status = response.status
                    self.send_response(status)
                    for field in ("Content-Type", "Retry-After", "request-id", "x-request-id"):
                        if response.getheader(field) is not None:
                            self.send_header(field, response.getheader(field))
                    self.end_headers()
                    response_started = True
                    with (directory / "response.raw").open("xb") as output:
                        os.fchmod(output.fileno(), 0o600)
                        while chunk := response.read1(65536):
                            output.write(chunk)
                            received += len(chunk)
                            if not client_disconnected:
                                try:
                                    self.wfile.write(chunk)
                                    self.wfile.flush()
                                except (BrokenPipeError, ConnectionResetError):
                                    client_disconnected = True
                    complete = status < 400
                except (OSError, http.client.HTTPException) as error:
                    failure = type(error).__name__
                    if not response_started:
                        try:
                            self.send_error(502)
                        except (BrokenPipeError, ConnectionResetError):
                            client_disconnected = True
                finally:
                    connection.close()
                    info = {
                        "schema_version": "skill-rsi.wire.v1", "request_sha256": digest(raw), "http_status": status,
                        "forwarded": forwarded, "complete": complete, "bytes": received, "failure": failure,
                        "client_disconnected": client_disconnected, "finished_at": _stamp(),
                    }
                    if (directory / "response.raw").is_file() and received <= _MAX_REQUEST:
                        response_raw = (directory / "response.raw").read_bytes()
                        info["response_sha256"] = digest(response_raw)
                        try:
                            if host == "claude_code":
                                parsed_response = decode_response(response_raw, status or 0)
                                info["response_complete"] = parsed_response["complete"]
                                info["usage"] = parsed_response.get("usage")
                                info["response_id"] = parsed_response.get("message_id")
                            else:
                                info.update(_responses_receipt(response_raw))
                        except (KeyError, TypeError, ValueError):
                            info["response_complete"] = None
                            info["response_parse_status"] = "unrecognized"
                    write(directory / "receipt.json", info)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/{self.token}"

    def serve(self):
        with self.server:
            self.server.serve_forever()


def _responses_receipt(raw):
    try:
        candidates = [json.loads(raw)]
    except (ValueError, UnicodeError):
        candidates = []
        for line in raw.splitlines():
            if line.startswith(b"data: "):
                try:
                    candidates.append(json.loads(line[6:]))
                except (ValueError, UnicodeError):
                    continue
    for row in reversed(candidates):
        row = mapping(row)
        if row.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
            response = mapping(row.get("response"))
            return {"response_complete": row["type"] == "response.completed", "usage": response.get("usage"),
                    "response_id": response.get("id")}
        if row.get("object") == "response":
            return {"response_complete": row.get("status") == "completed", "usage": row.get("usage"), "response_id": row.get("id")}
    return {"response_complete": None}


def read_wire(archive, source, host):
    if not source.is_dir():
        raise ValueError("Wire import needs a request or recorder directory")
    directories = [source] if (source / "request.raw").exists() else sorted(
        {path.parent for path in source.rglob("request.raw")} | {path.parent for path in source.rglob("intent.json")}
    )
    if not directories:
        raise ValueError(f"No wire capture at {source}")
    run = NativeRun({"batch_id": archive.root.name, "job_attempt_id": identifier(host, source.relative_to(archive.root).as_posix()),
                     "status": "unknown", "capture_capabilities": {"physical_requests": "observed_subset", "model_input": "unknown"}})
    for sequence, path in enumerate(directories, 1):
        data = {}
        for name, field, layer in (
            ("request.raw", "request", "wire_request"), ("request.json", "decoded", "gateway_ingress"),
            ("chat-request.json", "outbound", "wire_request_projection"),
            ("forward.json", "forward", "gateway_transmission"), ("receipt.json", "receipt", "gateway_receipt"),
            ("response.raw", "response", "wire_response"),
        ):
            candidate = archive.path(path / name)
            if candidate.is_file():
                data[field] = archive.ref(candidate, layer=layer)
        for key in ("forward", "receipt"):
            if key in data:
                run.sources.append(data[key])
        if "request" not in data:
            run.diagnostics.append({"reason": "missing_wire_request", "capture_id": path.name})
            continue
        raw = data["request"]
        receipt = archive.json(path / "receipt.json") if "receipt" in data else {}
        forward = archive.json(path / "forward.json") if "forward" in data else {}
        if not isinstance(receipt, dict) or not isinstance(forward, dict):
            raise ValueError(f"Invalid wire receipt in {path}")
        sent = ((forward.get("socket_write_observed") is True or type(receipt.get("http_status")) is int)
                and forward.get("request_sha256") == raw["sha256"])
        if receipt.get("request_sha256") not in {None, raw["sha256"]} or receipt.get("response_sha256") not in {
            None, mapping(data.get("response")).get("sha256"),
        }:
            run.diagnostics.append({"reason": "wire_receipt_hash_mismatch", "capture_id": path.name})
            sent = False
        request = event(raw, "model_request", sequence, actor_type="harness", logical_call_id=path.name, request_ref=raw, teacher_visible=False)
        run.events.append(request)
        observed = {**raw, "locator": pointer("/messages" if host == "claude_code" else "/input")}
        try:
            parsed = mapping(archive.json(path / "request.raw"))
        except (ValueError, UnicodeError):
            parsed = {}
        previous_response = mapping(parsed).get("previous_response_id")
        has_input = isinstance(mapping(parsed).get("messages" if host == "claude_code" else "input"), (list, str))
        # The recorded gateway body proves bytes leaving this boundary only.
        run.requests.append({
            "request_id": identifier(run.identity["job_attempt_id"], path.name), "logical_call_id": path.name,
            "attempt_id": path.name if sent else None, "request_ref": raw, "actual_context_ref": observed if sent and has_input and not previous_response else None,
            "logical_input_ref": observed if has_input and not previous_response else None,
            "response_ref": data.get("response"), "physical_request_status": "observed_at_gateway" if sent else "unknown",
            "status": "completed" if receipt.get("response_complete", receipt.get("complete")) is True else
                      "failed" if receipt.get("failure") or (receipt.get("http_status") or 0) >= 400 else "partial",
            "usage": receipt.get("usage") or receipt.get("final_usage"), "response_id": receipt.get("response_id"),
            "source_event_ids": [request["event_id"]], "usage_refs": [data["receipt"]] if "receipt" in data else [],
            "mapping_status": "unresolved",
            "missing_reasons": ["unreconstructed_previous_response_prefix"] if previous_response else [],
        })
    yield run


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", choices=sorted(_ROUTES), required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--credential-env")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    token = os.environ[args.credential_env] if args.credential_env else None
    recorder = WireRecorder(args.host, args.upstream, args.output, credential=token, port=args.port)
    print(json.dumps({"base_url": recorder.base_url}), flush=True)
    try:
        recorder.serve()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
