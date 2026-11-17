"""Project an editable subset of the Claude Messages history onto the next request."""

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

MAX_EDIT = 64 * 1024
MAX_NOTE = 8 * 1024


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def write_json(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
    try:
        temporary.write_bytes(encode(value) + b"\n")
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def blocks(message):
    content = message["content"]
    return [{"type": "text", "text": content}] if isinstance(content, str) else content


def exchange_id(pair):
    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items() if key != "cache_control"}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    return hashlib.sha256(encode(stable(pair))).hexdigest()[:24]


def editable_exchanges(messages):
    exchanges = {}
    duplicates = set()
    for index in range(1, len(messages) - 2):
        call, result = messages[index:index + 2]
        if (call["role"], result["role"]) != ("assistant", "user"):
            continue
        calls = [block["id"] for block in blocks(call) if block["type"] == "tool_use"]
        results = blocks(result)
        if not calls or any(
            block["type"] != "tool_result"
            and not (
                block["type"] == "text"
                and block["text"].strip().startswith("<system-reminder>")
                and block["text"].strip().endswith("</system-reminder>")
            )
            for block in results
        ):
            continue
        result_ids = [block["tool_use_id"] for block in results if block["type"] == "tool_result"]
        if len(calls) != len(set(calls)) or sorted(calls) != sorted(result_ids):
            continue
        pair = [call, result]
        key = exchange_id(pair)
        if key in exchanges:
            duplicates.add(key)
        exchanges[key] = (index, pair)
    return {key: item for key, item in exchanges.items() if key not in duplicates}


def project(messages, exchanges, dropped, note):
    omit = {
        position
        for key in dropped & exchanges.keys()
        for position in (exchanges[key][0], exchanges[key][0] + 1)
    }
    projected = [message for index, message in enumerate(messages) if index not in omit]
    if note:
        anchor = projected[0]
        projected[0] = {
            **anchor,
            "content": [
                *blocks(anchor),
                {"type": "text", "text": "CLM memory written by the assistant:\n" + note},
            ],
        }
    return projected


class Context:
    """One gateway instance owns one CLI session and consumes edits serially."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.revision = 0
        self.visible = set()
        self.dropped = set()
        self.note = ""
        self.anchor = None

    def _take_edit(self):
        incoming = self.directory / "edit.json"
        claimed = self.directory / (".edit-" + uuid.uuid4().hex)
        try:
            incoming.replace(claimed)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ValueError("invalid_edit") from error
        try:
            fd = os.open(claimed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                data = stream.read(MAX_EDIT + 1)
            if len(data) > MAX_EDIT:
                raise ValueError("edit_too_large")
            value = json.loads(data)
            if not isinstance(value, dict) or set(value) != {"revision", "drop", "note"}:
                raise ValueError("invalid_edit")
            return value
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid_edit") from error
        finally:
            if not claimed.is_dir():
                claimed.unlink(missing_ok=True)

    def apply(self, messages, *, accept_edit=True):
        if not messages or messages[0]["role"] != "user":
            raise ValueError("expected_initial_user_message")
        anchor = exchange_id(messages[0])
        if not accept_edit:
            preview = messages
            if anchor == self.anchor:
                preview = project(messages, editable_exchanges(messages), self.dropped, self.note)
            return preview, {"edit": None, "removed_this_request": len(messages) - len(preview)}
        if self.anchor is not None and anchor != self.anchor:
            # A native history reset cannot inherit an overlay from a different prefix.
            self.dropped.clear()
            self.note = ""
            self.visible.clear()
        self.anchor = anchor
        exchanges = editable_exchanges(messages)
        status = None
        applied_ids = []
        baseline = project(messages, exchanges, self.dropped, self.note)
        try:
            edit = self._take_edit()
            if edit is not None:
                if type(edit["revision"]) is not int or edit["revision"] != self.revision:
                    raise ValueError("stale_revision")
                drop, note = edit["drop"], edit["note"]
                if not isinstance(drop, list) or any(not isinstance(key, str) for key in drop):
                    raise ValueError("invalid_drop")
                drop = set(drop)
                if not drop <= self.visible & exchanges.keys():
                    raise ValueError("protected_or_unknown_exchange")
                if not isinstance(note, str) or len(note.encode()) > MAX_NOTE:
                    raise ValueError("invalid_note")
                candidate = project(messages, exchanges, self.dropped | drop, note)
                if len(encode(candidate)) >= len(encode(baseline)):
                    raise ValueError("edit_must_shrink")
                self.dropped.update(drop)
                self.note = note
                baseline = candidate
                applied_ids = sorted(drop)
                status = "accepted"
        except (ValueError, UnicodeError) as error:
            status = str(error)
        self.revision += 1
        self.visible = set(exchanges) - self.dropped
        write_json(self.directory / "LIVE_CTX_MAIN.json", {
            "revision": self.revision,
            "editable": [
                {"id": key, "bytes": len(encode(pair)), "messages": pair}
                for key, (_, pair) in exchanges.items() if key in self.visible
            ],
            "note": self.note,
            "last_edit": status,
        })
        return baseline, {
            "revision": self.revision,
            "edit": status,
            "dropped_ids": applied_ids,
            "removed_this_request": len(messages) - len(baseline),
            "editable_count": len(self.visible),
            "before_message_bytes": len(encode(messages)),
            "after_message_bytes": len(encode(baseline)),
        }
