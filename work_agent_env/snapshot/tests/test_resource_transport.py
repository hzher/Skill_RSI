"""Wire boundaries and uncertain result handling; no business service simulation claims."""
import asyncio
import io
import json
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from integrations.resource_transport import (
    MAX_REQUEST_FRAME_BYTES, MAX_RESPONSE_FRAME_BYTES, MAX_TOOL_RESPONSE_BYTES, encode_frame, wire_json)
from integrations.episode_worker_client import EpisodeWorkerClient
from integrations.nemo_http import NeMoHTTP
from workenv.core import ResourceError

try:
    from integrations import episode_worker as worker
except ImportError:
    worker = None


class FrameBudgets(unittest.TestCase):
    def test_full_tool_budget_survives_utf8_envelope_and_newline(self):
        result = {"content": [{"type": "text", "text": "\u83dc\U0001f35c\\\n\"" * 150000}]}
        padding = MAX_TOOL_RESPONSE_BYTES - len(wire_json(result))
        self.assertGreater(padding, 0)
        result["content"][0]["text"] += "a" * padding
        self.assertEqual(len(wire_json(result)), MAX_TOOL_RESPONSE_BYTES)
        response = {"id": str(uuid.uuid4()), "status": 200, "result": {"result": result}}
        encoded = encode_frame(response, MAX_RESPONSE_FRAME_BYTES)
        self.assertEqual(json.loads(encoded), response)
        self.assertLessEqual(len(encoded), MAX_RESPONSE_FRAME_BYTES)
        self.assertGreater(len(encoded), 3 * 1024 * 1024)

    def test_limit_includes_newline_and_escaped_text(self):
        value = {"text": "\u83dc\U0001f35c\\\n\""}
        raw = wire_json(value)
        self.assertEqual(encode_frame(value, len(raw) + 1), raw + b"\n")
        with self.assertRaises(ValueError): encode_frame(value, len(raw))
        for invalid in (float("nan"), {"text": "\ud800"}):
            with self.assertRaises((ValueError, UnicodeError)):
                encode_frame(invalid, MAX_RESPONSE_FRAME_BYTES)

    def test_reader_accepts_large_utf8_response_and_rejects_incomplete_frame(self):
        value = {"text": "\u83dc" * 1100000}
        for raw, valid in ((encode_frame(value, MAX_RESPONSE_FRAME_BYTES), True),
                           (wire_json(value), False), (b"a" * (MAX_RESPONSE_FRAME_BYTES + 1), False)):
            client = object.__new__(EpisodeWorkerClient)
            client.process = SimpleNamespace(stdout=io.BytesIO(raw))
            client.inbox = queue.Queue()
            client.timeout, client.poisoned = 1, False
            client._reader()
            if valid:
                self.assertEqual(client._receive(), value)
                self.assertFalse(client.poisoned)
            else:
                with self.assertRaises(ResourceError): client._receive()
                self.assertTrue(client.poisoned)

    def test_oversized_request_never_writes_or_consumes_frame_budget(self):
        client = object.__new__(EpisodeWorkerClient)
        client.lock, client.poisoned, client.frames = threading.Lock(), False, 0
        client.process = SimpleNamespace(stdin=io.BytesIO())
        with self.assertRaises(ValueError):
            client.call("/tool", {"text": "a" * MAX_REQUEST_FRAME_BYTES})
        self.assertEqual(client.process.stdin.getvalue(), b"")
        self.assertEqual(client.frames, 0)

    def test_http_reader_does_not_silently_accept_a_valid_prefix_of_oversized_reply(self):
        class Response(io.BytesIO):
            status = 200
        client = NeMoHTTP()
        response = {"text": "a" * (MAX_TOOL_RESPONSE_BYTES - 11)}
        # Accepted body is larger than the old fixed 4-MiB read once wrapped.
        body = {"result": response, "complete": True}
        self.assertGreater(len(wire_json(body)), MAX_TOOL_RESPONSE_BYTES)
        with patch.object(client.opener, "open", return_value=Response(wire_json(body))):
            self.assertEqual(client.post("/isolated/tool", {}), body)
        # JSON permits trailing whitespace, so parsing only a prefix would
        # otherwise silently accept this incomplete oversized HTTP response.
        with patch.object(client.opener, "open", return_value=Response(b"{}" + b" " * MAX_RESPONSE_FRAME_BYTES)):
            with self.assertRaisesRegex(RuntimeError, "outcome unknown"):
                client.post("/isolated/tool", {})


@unittest.skipIf(worker is None, "Original-task optional runtime dependencies unavailable")
class WorkerUnknownResponse(unittest.IsolatedAsyncioTestCase):
    async def test_untransportable_result_fences_replay_and_measurement_but_allows_close(self):
        for value in ({"text": "x" * MAX_RESPONSE_FRAME_BYTES}, {"value": float("nan")}, {"text": "\ud800"}):
            with self.subTest(kind=next(iter(value))):
                sid = str(uuid.uuid4())
                request_id = str(uuid.uuid4())
                frames = [{"id": str(uuid.uuid4()), "route": route,
                           "body": {"resources_session_id": sid, "request_id": request_id}}
                          for route in ("/tool", "/tool", "/verify", "/close_session")]
                incoming = io.BytesIO(b"".join(encode_frame(f, MAX_REQUEST_FRAME_BYTES) for f in frames))
                outgoing = io.BytesIO()
                resources = SimpleNamespace(active=None, dispatch=AsyncMock(side_effect=[value, {"resources_session_id": sid}]))
                with patch.object(worker.sys, "stdin", SimpleNamespace(buffer=incoming)), patch.object(worker.sys, "stdout", SimpleNamespace(buffer=outgoing)):
                    await worker.serve(resources, sid)
                replies = [json.loads(raw) for raw in outgoing.getvalue().splitlines()]
                self.assertEqual([r["status"] for r in replies], [503, 503, 503, 200])
                for reply in replies[:3]:
                    self.assertIsNone(reply["reward"])
                    self.assertTrue(reply["mask_sample"])
                self.assertTrue(replies[0]["outcome_unknown"])
                self.assertEqual([call.args[0] for call in resources.dispatch.await_args_list], ["/tool", "/close_session"])


if __name__ == "__main__": unittest.main()
