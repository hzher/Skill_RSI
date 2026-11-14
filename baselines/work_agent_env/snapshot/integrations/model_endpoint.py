"""Small, explicit endpoint probes. No office resources or training are accessed.

Responses stay in memory; the existing ledger stores response IDs and hashes only.
This module is deliberately not imported by the running NeMo resource bridge.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from integrations.model_usage import ModelUsageLedger

PROTOCOLS = ("openai_responses", "openai_chat", "anthropic_messages")
ROUTES = dict(zip(PROTOCOLS, ("/responses", "/chat/completions", "/messages")))
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
SCOPE = "endpoint_protocol_probe_not_office_task_acceptance"
ENV_NAME = r"[A-Z][A-Z0-9_]{0,99}"
IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}"
SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
          "required": ["a", "b"], "additionalProperties": False}
TOOL = {"name": "workenv_add", "description": "Return the sum of two integers.", "parameters": SCHEMA, "strict": True}
TOOL_PROMPT = "Call workenv_add with a=2 and b=3 exactly once. After receiving its result, reply with only 5."


class EndpointError(Exception):
    """Only fixed codes, never HTTP bodies, URLs, credentials or exception text."""
    def __init__(self, code, *, http_status=None, provider_error_code=None):
        super().__init__(code)
        self.code, self.http_status = code, http_status
        self.provider_error_code=provider_error_code


def provider_error_code(value):
    """Return fixed categories only; never retain provider messages or raw codes."""
    if not isinstance(value,dict):return None
    error=value.get('error',value)
    if not isinstance(error,dict):return None
    codes=(error.get('code'),error.get('type'))
    specific={'context_length_exceeded','insufficient_quota','rate_limit_exceeded','model_not_found',
              'overloaded_error','invalid_encrypted_content','authentication_error','permission_error'}
    for code in codes:
        if isinstance(code,str) and code in specific:return code
    message=error.get('message','')
    if isinstance(message,str):
        message=message[:4096].lower()
        for code,pattern in (
            ('context_limit',r'context.{0,30}(length|limit)|too many.{0,20}(input|prompt).{0,10}tokens'),
            ('reasoning_state_rejected',r'encrypted.{0,80}(invalid|decrypt|verif)'),
            ('tool_schema_rejected',r'(invalid|unsupported).{0,50}(tool|function).{0,20}schema'),
            ('account_quota',r'insufficient.{0,20}(quota|balance|credit)|quota exceeded'),
            ('provider_timeout',r'timed out|timeout'),('provider_overloaded',r'overload|capacity')):
            if re.search(pattern,message):return code
    for code in codes:
        if isinstance(code,str) and code in ('api_error','invalid_request_error','not_found_error'):return code
    return None


class ConfigurationMissing(EndpointError):
    def __init__(self, names):
        super().__init__("configuration_missing")
        self.names = sorted(set(names))


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError("duplicate")
            value[key] = item
        return value
    def constant(value):
        raise ValueError("nonfinite")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise EndpointError("invalid_json") from None


def _safe_id(value, secret):
    if not isinstance(value, str) or not re.fullmatch(IDENTIFIER, value) or secret in value or "sk-" in value.lower():
        raise EndpointError("unsafe_or_missing_identity")
    return value


@dataclass(frozen=True)
class EndpointSettings:
    protocol: str
    base_url: str = field(repr=False)
    model: str
    api_key: str = field(repr=False)
    max_tokens: int = 256
    timeout_seconds: int = 30
    chat_token_field: str = "max_completion_tokens"
    anthropic_version: str = "2023-06-01"
    reasoning_effort: str | None = None

    def __post_init__(self):
        if self.protocol not in PROTOCOLS: raise EndpointError("unsupported_protocol")
        if not isinstance(self.api_key, str) or not 8 <= len(self.api_key) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in self.api_key):
            raise EndpointError("invalid_credential")
        _safe_id(self.model, self.api_key)
        if not isinstance(self.base_url, str) or len(self.base_url) > 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in self.base_url):
            raise EndpointError("invalid_base_url")
        try:
            parsed = urlsplit(self.base_url)
            port = parsed.port
        except ValueError:
            raise EndpointError("invalid_base_url") from None
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.scheme not in ("https", "http") or port == 0 or "\\" in self.base_url
                or not re.fullmatch(r"[A-Za-z0-9/_\-]*", parsed.path)
                or parsed.path.rstrip("/").endswith("/v1/v1") or self.api_key in self.base_url):
            raise EndpointError("invalid_base_url")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1"):
            raise EndpointError("https_required")
        if type(self.max_tokens) is not int or not 16 <= self.max_tokens <= 8192:
            raise EndpointError("invalid_token_budget")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 60:
            raise EndpointError("invalid_timeout")
        if self.chat_token_field not in ("max_tokens", "max_completion_tokens"):
            raise EndpointError("invalid_token_field")
        if not isinstance(self.anthropic_version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.anthropic_version):
            raise EndpointError("invalid_api_version")
        if self.reasoning_effort not in (None, "low", "medium", "high", "xhigh", "max"):
            raise EndpointError("invalid_reasoning_effort")
        if self.protocol == "anthropic_messages" and self.reasoning_effort is not None:
            raise EndpointError("reasoning_not_supported_by_this_adapter")
        if self.model == "gpt-6.1-sol" and self.protocol != "openai_responses":
            raise EndpointError("responses_required_for_tool_probe")
        path = parsed.path.rstrip("/")
        if path and not path.endswith("/v1"):
            raise EndpointError("base_url_must_be_root_or_v1")

    @property
    def url(self):
        base = self.base_url.rstrip("/")
        return base + ("" if urlsplit(base).path.endswith("/v1") else "/v1") + ROUTES[self.protocol]


def load_settings(path, profile, environ=None):
    """Configuration contains variable names, never literal URLs, models or keys."""
    environ = os.environ if environ is None else environ
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", profile): raise EndpointError("invalid_profile")
    try:
        with Path(path).open("rb") as stream: raw = stream.read(16385)
    except OSError:
        raise EndpointError("configuration_unreadable") from None
    if len(raw) > 16384: raise EndpointError("configuration_too_large")
    doc = strict_json(raw)
    if not isinstance(doc, dict) or set(doc) != {"schema_version", "profiles"} or doc["schema_version"] != "model-endpoints-v1" or not isinstance(doc["profiles"], dict):
        raise EndpointError("invalid_configuration")
    row = doc["profiles"].get(profile)
    required = {"protocol", "base_url_env", "model_env", "api_key_env"}
    optional = {"max_tokens", "timeout_seconds", "chat_token_field", "anthropic_version", "reasoning_effort"}
    if not isinstance(row, dict) or not required <= set(row) or set(row) - required - optional:
        raise EndpointError("invalid_profile_configuration")
    refs = [row[name] for name in ("base_url_env", "model_env", "api_key_env")]
    if any(not isinstance(ref, str) or not re.fullmatch(ENV_NAME, ref) for ref in refs) or len(set(refs)) != 3:
        raise EndpointError("invalid_environment_reference")
    missing = [ref for ref in refs if not environ.get(ref)]
    if missing: raise ConfigurationMissing(missing)
    return EndpointSettings(protocol=row["protocol"], base_url=environ[refs[0]], model=environ[refs[1]], api_key=environ[refs[2]],
                            **{key: row[key] for key in optional if key in row})


def load_private_environment(root, path, ambient=None):
    """Read explicit private overrides without changing process-wide environment."""
    if ".." in Path(path).parts: raise EndpointError("private_environment_must_be_in_runtime")
    root, path = Path(root).absolute(), Path(path).absolute()
    if not path.is_relative_to(root / ".runtime"): raise EndpointError("private_environment_must_be_in_runtime")
    current = path
    while True:
        if current.is_symlink() or current.is_junction(): raise EndpointError("linked_private_environment")
        if current == root: break
        current = current.parent
    try:
        if path.stat().st_nlink != 1: raise EndpointError("linked_private_environment")
        with path.open("rb") as stream: raw = stream.read(16385)
    except OSError:
        raise EndpointError("private_environment_unreadable") from None
    if len(raw) > 16384: raise EndpointError("private_environment_too_large")
    values = strict_json(raw)
    if not isinstance(values, dict) or len(values) > 20 or any(
            not isinstance(k, str) or not re.fullmatch(ENV_NAME, k) or not isinstance(v, str) or len(v) > 4096
            for k, v in values.items()):
        raise EndpointError("invalid_private_environment")
    return {**(os.environ if ambient is None else ambient), **values}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class EndpointClient:
    def __init__(self, settings, ledger, *, max_requests=3, max_request_bytes=MAX_REQUEST_BYTES,
                 request_timeout_seconds=None,diagnose_http_errors=False):
        if type(max_requests) is not int or not 1 <= max_requests <= 100:
            raise EndpointError("invalid_request_budget")
        if type(max_request_bytes) is not int or not MAX_REQUEST_BYTES <= max_request_bytes <= 4 * 1024 * 1024:
            raise EndpointError("invalid_request_byte_budget")
        if request_timeout_seconds is not None and (type(request_timeout_seconds) is not int or not 1 <= request_timeout_seconds <= 300):
            raise EndpointError("invalid_request_timeout_budget")
        self.settings, self.ledger = settings, ledger
        self.max_requests, self.max_request_bytes = max_requests, max_request_bytes
        self.request_timeout_seconds = request_timeout_seconds
        if type(diagnose_http_errors) is not bool:raise EndpointError('invalid_error_diagnostic_mode')
        self.diagnose_http_errors=diagnose_http_errors
        # Do not silently route credentials through process-global proxy settings.
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())
        self.attempts, self.observations, self.http_responses = 0, [], 0

    def request(self, body):
        if self.attempts >= self.max_requests: raise EndpointError("request_budget_exhausted")
        raw = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > self.max_request_bytes: raise EndpointError("request_too_large")
        cfg = self.settings
        request_timeout = cfg.timeout_seconds if self.request_timeout_seconds is None else self.request_timeout_seconds
        if type(request_timeout) is not int or not 1 <= request_timeout <= 300:
            raise EndpointError("invalid_request_timeout_budget")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if body.get('stream') is True else "application/json"}
        if cfg.protocol == "anthropic_messages":
            headers.update({"x-api-key": cfg.api_key, "anthropic-version": cfg.anthropic_version})
        else:
            headers["Authorization"] = "Bearer " + cfg.api_key
        observation = self.ledger.begin(actor="executor", model_identity="endpoint_probe:" + cfg.protocol + ":" + cfg.model)
        self.attempts += 1
        started = time.monotonic()
        try:
            req = Request(cfg.url, data=raw, headers=headers, method="POST")
            with self.opener.open(req, timeout=request_timeout) as response:
                self.http_responses += 1
                if response.status != 200: raise EndpointError("unexpected_http_status", http_status=response.status)
                streaming=response.headers.get('Content-Type','').split(';',1)[0].strip().lower()=='text/event-stream'
                stream_metadata={}
                if streaming:
                    if body.get('stream') is not True:raise EndpointError('unexpected_stream_response')
                    from integrations.model_stream import read_message
                    value=read_message(response,cfg.protocol,deadline=started+request_timeout,metadata=stream_metadata)
                else:
                    value = bytearray()
                    while len(value) <= MAX_RESPONSE_BYTES:
                        if time.monotonic() - started >= request_timeout: raise EndpointError("timeout_outcome_unknown")
                        chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(value)))
                        if not chunk: break
                        value.extend(chunk)
                    if len(value) > MAX_RESPONSE_BYTES: raise EndpointError("response_too_large")
                    value = strict_json(value)
            if not isinstance(value, dict): raise EndpointError("invalid_response_object")
            identity = _safe_id(value.get("id"), cfg.api_key)
            observation.response(identity, value)
            usage = value.get("usage", {})
            usage = {k: v for k, v in usage.items() if k in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens")
                     and type(v) is int and 0 <= v <= 10**9} if isinstance(usage, dict) else {}
            self.observations.append({"response_id": identity, "usage": usage,
                                      "response_format":"sse" if streaming else "json",
                                      **stream_metadata,
                                      "elapsed_ms": round((time.monotonic() - started) * 1000)})
            return value
        except HTTPError as exc:
            self.http_responses += 1
            code = exc.code
            category=None
            if self.diagnose_http_errors and code not in (401,403) and not 300<=code<400:
                # Opt-in task diagnostics: bounded in-memory parsing; only a
                # fixed category escapes, never text, headers or arbitrary codes.
                try:
                    diagnostic=exc.read(16385)
                    if len(diagnostic)<=16384:category=provider_error_code(strict_json(diagnostic))
                except Exception:pass
            exc.close()
            name = ("redirect_refused" if 300 <= code < 400 else "authentication_failed" if code in (401, 403)
                    else "rate_limited" if code == 429 else "provider_error" if code >= 500 else "request_rejected")
            error = EndpointError(name, http_status=code,provider_error_code=category)
        except (TimeoutError, socket.timeout):
            error = EndpointError("timeout_outcome_unknown")
        except URLError as exc:
            error = EndpointError("timeout_outcome_unknown" if isinstance(exc.reason, TimeoutError)
                                  else "tls_validation_failed" if isinstance(exc.reason, ssl.SSLError) else "transport_outcome_unknown")
        except EndpointError as exc:
            error = exc
        except HTTPException:
            error = EndpointError("transport_outcome_unknown")
        except (OSError, ValueError, RecursionError):
            error = EndpointError("transport_or_encoding_error")
        if observation.value["status"] == "started": observation.failed(error.code)
        raise error from None


def initial_body(cfg, *, tools=False):
    prompt = TOOL_PROMPT if tools else "Reply with only WORKENV_OK."
    body = {"model": cfg.model, "stream": False}
    if cfg.protocol == "openai_responses":
        body.update(input=[{"role": "user", "content": prompt}], max_output_tokens=cfg.max_tokens, store=False)
        if cfg.reasoning_effort is not None: body["reasoning"] = {"effort": cfg.reasoning_effort}
        if tools:
            body.update(tools=[{"type": "function", **TOOL}], tool_choice={"type": "function", "name": TOOL["name"]},
                        parallel_tool_calls=False, include=["reasoning.encrypted_content"])
    else:
        body.update(messages=[{"role": "user", "content": prompt}])
        body["max_tokens" if cfg.protocol == "anthropic_messages" else cfg.chat_token_field] = cfg.max_tokens
        if cfg.protocol == "openai_chat" and cfg.reasoning_effort is not None: body["reasoning_effort"] = cfg.reasoning_effort
        if tools and cfg.protocol == "openai_chat":
            body.update(tools=[{"type": "function", "function": TOOL}],
                        tool_choice={"type": "function", "function": {"name": TOOL["name"]}}, parallel_tool_calls=False)
        elif tools:
            body.update(tools=[{"name": TOOL["name"], "description": TOOL["description"], "input_schema": SCHEMA}],
                        tool_choice={"type": "auto", "disable_parallel_tool_use": True})
    return body


def _items(value):
    if not isinstance(value, list) or len(value) > 64 or any(not isinstance(item, dict) for item in value):
        raise EndpointError("invalid_response_shape")
    return value


def _chat_message(value):
    choices = _items(value.get("choices"))
    if len(choices) != 1 or not isinstance(choices[0].get("message"), dict): raise EndpointError("invalid_response_shape")
    message = choices[0]["message"]
    if message.get("role") != "assistant": raise EndpointError("invalid_response_shape")
    return choices[0], message


def response_text(cfg, value):
    if cfg.protocol == "openai_chat":
        choice, message = _chat_message(value)
        if choice.get("finish_reason") != "stop" or message.get("tool_calls"): raise EndpointError("incomplete_text_response")
        text = message.get("content")
    elif cfg.protocol == "openai_responses":
        if value.get("status") != "completed": raise EndpointError("incomplete_text_response")
        output = _items(value.get("output"))
        if any(item.get("type") not in ("reasoning", "message") for item in output): raise EndpointError("unexpected_output")
        blocks = [part for item in output if item.get("type") == "message" for part in _items(item.get("content"))]
        if any(part.get("type") != "output_text" or not isinstance(part.get("text"), str) for part in blocks): raise EndpointError("unexpected_output")
        text = "".join(part["text"] for part in blocks)
    else:
        if value.get("stop_reason") != "end_turn": raise EndpointError("incomplete_text_response")
        blocks = _items(value.get("content"))
        if any(part.get("type") not in ("text", "thinking", "redacted_thinking") for part in blocks): raise EndpointError("unexpected_output")
        if any(part.get("type") == "text" and not isinstance(part.get("text"), str) for part in blocks): raise EndpointError("invalid_text_response")
        text = "".join(part["text"] for part in blocks if part.get("type") == "text")
    if not isinstance(text, str): raise EndpointError("invalid_text_response")
    return text.strip()


def tool_continuation(cfg, request, response):
    if cfg.protocol == "openai_chat":
        choice, message = _chat_message(response)
        calls = _items(message.get("tool_calls"))
        if choice.get("finish_reason") != "tool_calls" or len(calls) != 1 or calls[0].get("type") != "function":
            raise EndpointError("invalid_tool_call")
        call = calls[0]
        function = call.get("function")
        if not isinstance(function, dict): raise EndpointError("invalid_tool_call")
        name, arguments, identity = function.get("name"), function.get("arguments"), call.get("id")
    elif cfg.protocol == "openai_responses":
        if response.get("status") != "completed": raise EndpointError("invalid_tool_call")
        output = _items(response.get("output"))
        if any(item.get("type") not in ("function_call", "reasoning", "message") for item in output): raise EndpointError("unexpected_output")
        calls = [item for item in output if item.get("type") == "function_call"]
        if len(calls) != 1: raise EndpointError("invalid_tool_call")
        call = calls[0]
        name, arguments, identity = call.get("name"), call.get("arguments"), call.get("call_id")
    else:
        content = _items(response.get("content"))
        if any(item.get("type") not in ("tool_use", "text", "thinking", "redacted_thinking") for item in content):
            raise EndpointError("unexpected_output")
        calls = [item for item in content if item.get("type") == "tool_use"]
        if response.get("stop_reason") != "tool_use" or len(calls) != 1: raise EndpointError("invalid_tool_call")
        call = calls[0]
        name, arguments, identity = call.get("name"), call.get("input"), call.get("id")
    identity = _safe_id(identity, cfg.api_key)
    if cfg.protocol != "anthropic_messages":
        if not isinstance(arguments, str) or len(arguments) > 1024: raise EndpointError("invalid_tool_arguments")
        arguments = strict_json(arguments)
    if name != TOOL["name"] or not isinstance(arguments, dict) or set(arguments) != {"a", "b"} or any(type(arguments[k]) is not int for k in ("a", "b")) or arguments != {"a": 2, "b": 3}:
        raise EndpointError("invalid_tool_arguments")
    result = str(arguments["a"] + arguments["b"])
    body = {**request}
    if cfg.protocol == "openai_chat":
        assistant = {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
        body["messages"] = [*request["messages"], assistant, {"role": "tool", "tool_call_id": identity, "content": result}]
        body["tool_choice"] = "none"
    elif cfg.protocol == "openai_responses":
        body["input"] = [*request["input"], *output, {"type": "function_call_output", "call_id": identity, "output": result}]
        body["tool_choice"] = "none"
    else:
        body["messages"] = [*request["messages"], {"role": "assistant", "content": content},
                            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": identity, "content": result}]}]
        body["tool_choice"] = {"type": "none"}
    return body


def run_probe(settings, directory,*,stream=False):
    """At most three POSTs; any failure stops this attempt without replay."""
    directory = Path(directory).absolute()
    if type(stream) is not bool or stream and settings.protocol=='openai_chat':raise EndpointError('unsupported_stream_protocol')
    for path in (directory, *directory.parents):
        if path.is_symlink() or path.is_junction(): raise EndpointError("linked_evidence_directory")
    directory.mkdir(parents=True, exist_ok=False)
    ledger = ModelUsageLedger(directory / "transport")
    client = EndpointClient(settings, ledger)
    report = {"schema_version": "model-endpoint-probe-v1", "scope": SCOPE, "probe_id": directory.name,
              "started_at": datetime.now(timezone.utc).isoformat(),
              "protocol": settings.protocol, "requested_model": settings.model,
              "endpoint_sha256": hashlib.sha256(settings.base_url.encode()).hexdigest(),
              "max_output_tokens_per_request": settings.max_tokens, "request_limit": 3, "reasoning_effort": settings.reasoning_effort,
              "phases": [], "status": "failed", "office_episodes": 0, "training_updates": 0,
              "model_inference_attested": None}
    report['stream_requested']=stream
    phase = "conversation"
    try:
        first = client.request({**initial_body(settings),'stream':stream})
        if response_text(settings, first) != "WORKENV_OK": raise EndpointError("unexpected_conversation_answer")
        report["phases"].append({"name": phase, "passed": True})
        phase = "tool_call"
        request = initial_body(settings, tools=True)
        request['stream']=stream
        second = client.request(request)
        continuation = tool_continuation(settings, request, second)
        report["phases"].append({"name": phase, "passed": True})
        phase = "tool_result_return"
        third = client.request(continuation)
        if response_text(settings, third) != "5": raise EndpointError("unexpected_final_answer")
        report["phases"].append({"name": phase, "passed": True})
        report["status"] = "passed"
    except EndpointError as exc:
        if exc.code.endswith("outcome_unknown"): report["status"] = "unknown"
        report["phases"].append({"name": phase, "passed": False, "error_code": exc.code, "http_status": exc.http_status})
    report.update(http_requests_attempted=client.attempts, http_responses_received=client.http_responses, responses=client.observations,
                  finished_at=datetime.now(timezone.utc).isoformat())
    # Deliberately no request/response bodies, exception strings, addresses or headers.
    (directory / "summary.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report
