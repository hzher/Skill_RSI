"""UTF-8 wire budgets, independent of persisted request fingerprint encoding."""
import json

MAX_REQUEST_FRAME_BYTES = 3 * 1024 * 1024
MAX_TOOL_RESPONSE_BYTES = 4 * 1024 * 1024
# Tool result + resource result + transport id/status + terminating newline.
# The whole frame is checked as well, so this allowance is never implicit.
MAX_RESPONSE_FRAME_BYTES = MAX_TOOL_RESPONSE_BYTES + 1024


def wire_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode_frame(value, limit):
    raw = wire_json(value) + b"\n"
    if len(raw) > limit:
        raise ValueError("Resource frame exceeds its encoded byte budget")
    return raw
