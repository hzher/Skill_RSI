"""Protocol and transport boundaries against local fixtures, never model APIs."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

from integrations.model_endpoint import (ConfigurationMissing, EndpointClient, EndpointError, EndpointSettings,
    MAX_RESPONSE_BYTES, initial_body, load_private_environment, load_settings, response_text, run_probe,
    strict_json, tool_continuation)
from integrations.model_usage import ModelUsageLedger
from scripts import verify_model_endpoint

KEY = "unit-test-credential-only"


def fixture(protocol, phase):
    text = "WORKENV_OK" if phase == 0 else "5"
    base = {"id": "unit-response-" + str(phase), "usage": {"input_tokens": 12, "output_tokens": 7}}
    if protocol == "openai_chat":
        message = {"role": "assistant", "content": text}
        choice = {"index": 0, "finish_reason": "stop", "message": message}
        if phase == 1:
            message.update(content=None, tool_calls=[{"type": "function", "id": "call-unit-1", "function": {
                "name": "workenv_add", "arguments": '{"a":2,"b":3}'}}])
            choice["finish_reason"] = "tool_calls"
        return {**base, "choices": [choice]}
    if protocol == "openai_responses":
        output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]
        if phase == 1:
            output = [{"type": "reasoning", "id": "rs-unit", "summary": [], "encrypted_content": "unit-ciphertext"},
                      {"type": "function_call", "id": "fc-unit", "call_id": "call-unit-1", "name": "workenv_add", "arguments": '{"a":2,"b":3}'}]
        return {**base, "status": "completed", "output": output}
    content = [{"type": "text", "text": text}]
    if phase == 1:
        content = [{"type": "thinking", "thinking": "unit fixture", "signature": "unit-signature"},
                   {"type": "tool_use", "id": "toolu-unit", "name": "workenv_add", "input": {"a": 2, "b": 3}}]
    return {**base, "type": "message", "role": "assistant", "stop_reason": "tool_use" if phase == 1 else "end_turn", "content": content}


@contextmanager
def server(replies):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            requests.append({"path": self.path, "headers": dict(self.headers),
                             "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])) )})
            index = len(requests) - 1
            status, body, headers = replies[min(index, len(replies) - 1)]
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            for key, value in headers.items(): self.send_header(key, value)
            self.end_headers()
            try: self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError): pass
    host = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=host.serve_forever, daemon=True)
    thread.start()
    try: yield "http://127.0.0.1:" + str(host.server_port), requests
    finally:
        host.shutdown()
        host.server_close()
        thread.join(timeout=2)


class ModelEndpointTests(unittest.TestCase):
    def test_long_task_timeout_is_explicit_and_probe_default_stays_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg=EndpointSettings('openai_responses','https://unit.invalid','unit',KEY)
            ledger=ModelUsageLedger(Path(folder))
            self.assertIsNone(EndpointClient(cfg,ledger).request_timeout_seconds)
            self.assertEqual(EndpointClient(cfg,ledger,request_timeout_seconds=300).request_timeout_seconds,300)
            client=EndpointClient(cfg,ledger,request_timeout_seconds=300)
            client.opener=unittest.mock.Mock()
            client.opener.open.side_effect=URLError(socket.timeout())
            with self.assertRaises(EndpointError):client.request({'model':'unit'})
            self.assertEqual(client.opener.open.call_args.kwargs['timeout'],300)
            self.assertEqual(client.attempts,1)
            self.assertEqual(cfg.timeout_seconds,30)
            for value in (True,0,301,'300'):
                with self.subTest(value=value),self.assertRaises(EndpointError):
                    EndpointClient(cfg,ledger,request_timeout_seconds=value)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = EndpointSettings("openai_responses", "https://unit.invalid/v1", "unit-model", KEY)

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(EndpointError) as result: function(*args, **kwargs)
        self.assertEqual(result.exception.code, code)

    def test_root_and_versioned_urls_have_one_v1(self):
        for base in ("https://unit.invalid", "https://unit.invalid/", "https://unit.invalid/v1", "https://unit.invalid/v1/"):
            self.assertEqual(replace(self.cfg, base_url=base).url, "https://unit.invalid/v1/responses")

    def test_unsafe_endpoints_are_rejected_before_network(self):
        for base in ("http://example.invalid", "https://user:password@unit.invalid/v1", "https://unit.invalid/v1?key=x",
                     "https://unit.invalid/v1#secret", "https://unit.invalid/v1/v1", "https://unit.invalid/v1/responses",
                     "https://unit.invalid/../v1", "https://unit.invalid/%76%31", "https://unit.invalid/\n"):
            with self.subTest(base=base), self.assertRaises(EndpointError): replace(self.cfg, base_url=base)

    def test_credentials_are_not_in_representations_or_errors(self):
        self.assertNotIn(KEY, repr(self.cfg))
        self.assertNotIn("unit.invalid", repr(self.cfg))
        self.assertCode("invalid_credential", replace, self.cfg, api_key="bad\nheader")

    def test_budgets_are_bounded_integers(self):
        for name, values in (("max_tokens", (True, "8192", 0, 8193)), ("timeout_seconds", (False, 0, 61, "60"))):
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(EndpointError): replace(self.cfg, **{name: value})
        self.assertEqual(replace(self.cfg, max_tokens=8192).max_tokens, 8192)

    def test_gpt_61_tool_probe_requires_responses_and_keeps_reasoning(self):
        self.assertCode("responses_required_for_tool_probe", replace, self.cfg, model="gpt-6.1-sol", protocol="openai_chat")
        cfg = replace(self.cfg, model="gpt-6.1-sol", reasoning_effort="medium", max_tokens=8192)
        body = initial_body(cfg, tools=True)
        self.assertEqual(body["reasoning"], {"effort": "medium"})
        self.assertEqual(body["max_output_tokens"], 8192)
        self.assertFalse(body["store"])
        self.assertCode("invalid_reasoning_effort", replace, self.cfg, reasoning_effort="none")

    def test_legacy_chat_token_field_is_explicit(self):
        body = initial_body(replace(self.cfg, protocol="openai_chat", chat_token_field="max_tokens"))
        self.assertIn("max_tokens", body)
        self.assertNotIn("max_completion_tokens", body)
        self.assertCode("invalid_token_field", replace, self.cfg, chat_token_field="anything")

    def test_claude_55_never_forces_tools_or_disables_thinking(self):
        cfg = replace(self.cfg, protocol="anthropic_messages", model="claude-opus-5-5")
        body = initial_body(cfg, tools=True)
        self.assertEqual(body["tool_choice"], {"type": "auto", "disable_parallel_tool_use": True})
        self.assertNotIn("thinking", body)
        original = fixture("anthropic_messages", 1)
        continuation = tool_continuation(cfg, body, original)
        self.assertEqual(continuation["messages"][-2]["content"], original["content"])

    def test_ambiguous_json_and_nonfinite_numbers_are_refused(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', b'\xff', '{'):
            with self.subTest(raw=raw): self.assertCode("invalid_json", strict_json, raw)

    def test_template_contains_no_endpoint_credentials_or_model_guesses(self):
        path = Path(__file__).resolve().parents[1] / "configs/model_endpoints.example.json"
        with self.assertRaises(ConfigurationMissing) as result: load_settings(path, "openai", {})
        self.assertEqual(set(result.exception.names), {"WORKENV_OPENAI_BASE_URL", "WORKENV_OPENAI_MODEL", "OPENAI_API_KEY"})
        cfg = load_settings(path, "openai", {"WORKENV_OPENAI_BASE_URL": "https://unit.invalid/v1", "WORKENV_OPENAI_MODEL": "gpt-6.1-sol", "OPENAI_API_KEY": KEY})
        self.assertEqual(cfg.reasoning_effort, "medium")

    def test_config_rejects_literal_key_field_and_duplicate_json(self):
        path = self.root / "config.json"
        doc = {"schema_version": "model-endpoints-v1", "profiles": {"unit": {"protocol": "openai_chat", "base_url_env": "BASE", "model_env": "MODEL", "api_key_env": "KEY", "api_key": KEY}}}
        path.write_text(json.dumps(doc))
        self.assertCode("invalid_profile_configuration", load_settings, path, "unit", {})
        path.write_text('{"profiles":{},"profiles":{}}')
        self.assertCode("invalid_json", load_settings, path, "unit", {})

    def test_private_environment_is_explicit_and_does_not_change_ambient(self):
        path = self.root / ".runtime/private.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"UNIT_KEY": KEY}))
        ambient = {"KEEP": "unit"}
        result = load_private_environment(self.root, path, ambient)
        self.assertEqual(result["UNIT_KEY"], KEY)
        self.assertEqual(ambient, {"KEEP": "unit"})
        self.assertCode("private_environment_must_be_in_runtime", load_private_environment, self.root, self.root / "private.json")
        self.assertCode("private_environment_must_be_in_runtime", load_private_environment, self.root, path.parent / ".." / "private.json")

    def test_private_environment_rejects_duplicates_and_nonstring_values(self):
        path = self.root / ".runtime/private.json"
        path.parent.mkdir()
        for value, code in (('{"KEY":"a","KEY":"b"}', "invalid_json"), ('{"KEY":2}', "invalid_private_environment")):
            path.write_text(value)
            self.assertCode(code, load_private_environment, self.root, path)

    def test_private_environment_rejects_hardlinks(self):
        path = self.root / ".runtime/private.json"
        path.parent.mkdir()
        path.write_text('{}')
        (path.parent / "linked.json").hardlink_to(path)
        self.assertCode("linked_private_environment", load_private_environment, self.root, path)

    def check_protocol(self, protocol, route):
        replies = [(200, fixture(protocol, i), {}) for i in range(3)]
        with server(replies) as (base, requests):
            result = run_probe(replace(self.cfg, base_url=base, protocol=protocol), self.root / "probe")
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["http_requests_attempted"], 3)
        self.assertEqual(result["office_episodes"], 0)
        self.assertEqual(result["training_updates"], 0)
        self.assertTrue(all(row["path"] == route for row in requests))
        headers = {k.lower(): v for k, v in requests[0]["headers"].items()}
        if protocol == "anthropic_messages":
            self.assertEqual(headers["x-api-key"], KEY)
            self.assertNotIn("authorization", headers)
            body = requests[2]["body"]
            self.assertEqual(body["messages"][-2]["content"][0]["signature"], "unit-signature")
            self.assertEqual(body["messages"][-1]["content"][0]["tool_use_id"], "toolu-unit")
        elif protocol == "openai_responses":
            self.assertEqual(headers["authorization"], "Bearer " + KEY)
            body = requests[2]["body"]
            self.assertEqual(body["input"][1]["encrypted_content"], "unit-ciphertext")
            self.assertEqual(body["input"][-1], {"type": "function_call_output", "call_id": "call-unit-1", "output": "5"})
        else:
            self.assertEqual(requests[2]["body"]["messages"][-1], {"role": "tool", "tool_call_id": "call-unit-1", "content": "5"})
        saved = "\n".join(p.read_text() for p in (self.root / "probe").rglob("*.json"))
        for secret in (KEY, "unit-ciphertext", "unit-signature", "Reply with only"):
            self.assertNotIn(secret, saved)

    def test_responses_full_three_request_protocol(self): self.check_protocol("openai_responses", "/v1/responses")
    def test_chat_full_three_request_protocol(self): self.check_protocol("openai_chat", "/v1/chat/completions")
    def test_claude_full_three_request_protocol(self): self.check_protocol("anthropic_messages", "/v1/messages")

    def test_http_errors_are_classified_without_body_or_retry(self):
        for status, code in ((400, "request_rejected"), (401, "authentication_failed"), (403, "authentication_failed"), (429, "rate_limited"), (503, "provider_error")):
            with self.subTest(status=status), server([(status, {"error": KEY}, {})]) as (base, requests):
                result = run_probe(replace(self.cfg, base_url=base), self.root / str(status))
                self.assertEqual(len(requests), 1)
                self.assertEqual(result["phases"][-1]["error_code"], code)
                self.assertEqual(result["phases"][-1]["http_status"], status)
                self.assertNotIn(KEY, json.dumps(result))

    def test_redirect_does_not_forward_credentials_or_replay(self):
        with server([(200, fixture("openai_responses", 0), {})]) as (other, other_requests):
            with server([(307, b"", {"Location": other + "/v1/responses"})]) as (base, requests):
                result = run_probe(replace(self.cfg, base_url=base), self.root / "redirect")
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(other_requests), 0)
        self.assertEqual(result["phases"][-1]["error_code"], "redirect_refused")

    def test_response_size_limit_is_enforced(self):
        with server([(200, b"x" * (MAX_RESPONSE_BYTES + 1), {})]) as (base, requests):
            result = run_probe(replace(self.cfg, base_url=base), self.root / "large")
        self.assertEqual(result["phases"][-1]["error_code"], "response_too_large")
        self.assertEqual(len(requests), 1)

    def test_invalid_json_response_is_not_a_success(self):
        with server([(200, b'{"id":"one","id":"two"}', {})]) as (base, requests):
            result = run_probe(replace(self.cfg, base_url=base), self.root / "json")
        self.assertEqual(result["phases"][-1]["error_code"], "invalid_json")

    def test_server_echoing_a_secret_identity_is_not_persisted(self):
        for index, identity in enumerate((KEY, "prefix-" + KEY, "sk-unit-echo")):
            body = fixture("openai_responses", 0)
            body["id"] = identity
            with server([(200, body, {})]) as (base, requests):
                directory = self.root / ("echo" + str(index))
                result = run_probe(replace(self.cfg, base_url=base), directory)
            self.assertEqual(result["phases"][-1]["error_code"], "unsafe_or_missing_identity")
            for path in directory.rglob("*.json"): self.assertNotIn(identity, path.read_text())

    def test_transport_timeout_is_unknown_and_does_not_retry(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = URLError(socket.timeout())
        with patch("integrations.model_endpoint.build_opener", return_value=opener):
            result = run_probe(self.cfg, self.root / "timeout")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["http_requests_attempted"], 1)
        self.assertEqual(opener.open.call_count, 1)

    def test_incomplete_reasoning_output_does_not_pass(self):
        value = fixture("openai_responses", 0)
        value["status"] = "incomplete"
        self.assertCode("incomplete_text_response", response_text, self.cfg, value)

    def test_wrong_answer_stops_before_tool_call(self):
        body = fixture("openai_responses", 0)
        body["output"][0]["content"][0]["text"] = "not the requested marker"
        with server([(200, body, {})]) as (base, requests):
            result = run_probe(replace(self.cfg, base_url=base), self.root / "wrong")
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["status"], "failed")

    def test_tool_arguments_reject_wrong_types_extras_and_duplicates(self):
        for arguments in ('{"a":true,"b":3}', '{"a":2,"b":3,"c":0}', '{"a":2,"a":2,"b":3}', '{"a":2,"b":"3"}', '{"a":-2,"b":3}'):
            value = fixture("openai_responses", 1)
            value["output"][1]["arguments"] = arguments
            with self.subTest(arguments=arguments), self.assertRaises(EndpointError): tool_continuation(self.cfg, initial_body(self.cfg, tools=True), value)

    def test_multiple_or_unknown_tools_are_rejected(self):
        for mode in ("multiple", "unknown"):
            value = fixture("openai_responses", 1)
            if mode == "multiple": value["output"].append(deepcopy(value["output"][1]))
            else: value["output"][1]["name"] = "execute_shell"
            with self.subTest(mode=mode), self.assertRaises(EndpointError): tool_continuation(self.cfg, initial_body(self.cfg, tools=True), value)

    def test_unbounded_request_and_fourth_call_are_blocked(self):
        client = EndpointClient(self.cfg, ModelUsageLedger(self.root / "ledger"))
        self.assertCode("request_too_large", client.request, {"text": "x" * 70000})
        self.assertEqual(client.attempts, 0)
        client.attempts = 3
        self.assertCode("request_budget_exhausted", client.request, {})

    def test_existing_evidence_directory_is_never_overwritten(self):
        directory = self.root / "existing"
        directory.mkdir()
        with self.assertRaises(FileExistsError): run_probe(self.cfg, directory)

    def test_usage_is_allowlisted_and_cannot_leak_raw_fields(self):
        body = fixture("openai_responses", 0)
        body["usage"] = {"input_tokens": 3, "output_tokens": True, "total_tokens": -1, "secret": KEY}
        with server([(200, body, {}), (503, b"", {})]) as (base, requests):
            result = run_probe(replace(self.cfg, base_url=base), self.root / "usage")
        self.assertEqual(result["responses"][0]["usage"], {"input_tokens": 3})
        self.assertNotIn(KEY, json.dumps(result))

    def test_cli_default_preflight_does_not_construct_a_network_client(self):
        environment = {"WORKENV_OPENAI_BASE_URL": "https://unit.invalid", "WORKENV_OPENAI_MODEL": "gpt-6.1-sol", "OPENAI_API_KEY": KEY}
        output = io.StringIO()
        with patch.dict("os.environ", environment, clear=True), patch("integrations.model_endpoint.build_opener") as opener, patch("sys.stdout", output):
            code = verify_model_endpoint.main(["--profile", "openai"])
        self.assertEqual(code, 0)
        opener.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["http_requests_attempted"], 0)
        self.assertNotIn(KEY, output.getvalue())


if __name__ == "__main__": unittest.main()
