import json
import unittest

from skillrl.wire import StreamTiming, decode_response


def sse(event):
    return b"data: " + json.dumps(event).encode() + b"\n\n"


class WireTests(unittest.TestCase):
    def test_stop_marker_without_finish_and_usage_remains_unknown(self):
        raw = sse({"type": "message_start", "message": {"model": "m", "usage": {"input_tokens": 4}}})
        raw += sse({"type": "message_delta", "delta": {}}) + sse({"type": "message_stop"})
        self.assertFalse(decode_response(raw, 200)["complete"])

    def test_complete_stream_preserves_input_and_final_output_usage(self):
        raw = sse({"type": "message_start", "message": {"model": "m", "usage": {"input_tokens": 4, "output_tokens": 1}}})
        raw += sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}})
        raw += sse({"type": "message_stop"})
        result = decode_response(raw, 200)
        self.assertTrue(result["complete"])
        self.assertEqual(result["usage"], {"input_tokens": 4, "output_tokens": 9})
        partial = decode_response(raw.split(b'data: {"type": "message_delta"')[0], 200)
        self.assertEqual(partial["disposition"], "unknown")
        self.assertIsNone(partial["usage"])

    def test_mixed_frame_delimiters_retain_both_events(self):
        timing = StreamTiming(10)
        raw = sse({"type": "message_start", "message": {"model": "m"}})
        raw += sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}).replace(b"\n", b"\r\n")
        timing.feed(raw, 11)
        self.assertEqual(len(timing.events), 2)
        self.assertIsNone(timing.summary(10, 2, .1)["observed_stream_output_tokens_per_second"])
