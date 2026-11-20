import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from skillrl.messages import RecordingBridge, native_contract


class MessagesTests(unittest.TestCase):
    def test_forwarded_bytes_and_partial_usage_are_retained(self):
        incoming = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                incoming.append(raw)
                if len(incoming) == 1:
                    body = json.dumps({"type": "message", "id": "m1", "model": "wanted", "content": [], "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 2}}).encode()
                else:
                    body = b'data: {"type":"message_start","message":{"id":"m2","model":"wanted","usage":{"input_tokens":3}}}\n\n'
                self.send_response(200)
                self.send_header("Content-Type", "application/json" if len(incoming) == 1 else "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as tmp:
            bridge = RecordingBridge(f"http://127.0.0.1:{upstream.server_port}/v1", "test-secret", "wanted", tmp)
            port = bridge.start()
            try:
                for number in range(2):
                    payload = {"model": "wanted", "system": "You are Claude Code\n# Harness\n# Environment\n# Context management", "tools": [{"name": "mcp__benchmark__act"}, {"name": "mcp__benchmark__status"}], "messages": [{"role": "user", "content": str(number)}]}
                    raw = json.dumps(payload, indent=2).encode()
                    request = urllib.request.Request(f"http://127.0.0.1:{port}/{bridge.token}/v1/messages", data=raw)
                    with urllib.request.urlopen(request) as response:
                        response.read()
                    self.assertEqual(incoming[-1], raw)
            finally:
                bridge.close()
                upstream.shutdown()
                upstream.server_close()
                thread.join()
            first = json.loads((Path(tmp) / "0000/receipt.json").read_text())
            partial = json.loads((Path(tmp) / "0001/receipt.json").read_text())
            self.assertTrue(first["complete"])
            self.assertEqual(partial["disposition"], "unknown")
            self.assertIsNone(partial["usage"])
            self.assertTrue((Path(tmp) / "0001/response.raw").read_bytes())

    def test_native_contract_requires_full_prompt_and_real_task_tools(self):
        request = {"system": "You are Claude Code, Anthropic's official CLI for Claude.\n# Harness\n# Environment\n# Context management", "tools": [{"name": "mcp__benchmark__act"}, {"name": "mcp__benchmark__status"}]}
        self.assertTrue(native_contract(request)["accepted"])
        request["system"] = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
        self.assertFalse(native_contract(request)["accepted"])
        request["system"] += "\n# Harness\n# Environment\n# Context management"
        self.assertTrue(native_contract(request)["accepted"])

    def test_rejected_route_and_model_are_recorded_before_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = RecordingBridge("http://127.0.0.1:1/v1", "test-secret", "wanted", Path(tmp), bind="127.0.0.1")
            port = bridge.start()
            try:
                for route, model, status in [("wrong", "wanted", 404), (bridge.token + "/v1/messages", "other", 400)]:
                    request = urllib.request.Request(f"http://127.0.0.1:{port}/{route}", data=json.dumps({"model": model}).encode())
                    with self.assertRaises(urllib.error.HTTPError) as context:
                        urllib.request.urlopen(request)
                    self.assertEqual(context.exception.code, status)
                    context.exception.close()
            finally:
                bridge.close()
            receipts = list(Path(tmp).glob("*/receipt.json"))
            self.assertEqual(len(receipts), 2)
            self.assertTrue(all(not json.loads(path.read_text())["forwarded"] for path in receipts))
            self.assertTrue(all((path.parent / "request.raw").exists() for path in receipts))


if __name__ == "__main__":
    unittest.main()
