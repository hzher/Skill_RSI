"""Project complete tool exchanges out of a stateless Responses input."""

import collections
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path

CALLS = {"function_call": "function_call_output", "custom_tool_call": "custom_tool_call_output"}
OUTPUTS = set(CALLS.values())
NOTE_LIMIT = 8192
EDIT_LIMIT = 65536
EDIT_ID = re.compile(r"[0-9a-f]{24}")


def encode(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def digest(data):
    return hashlib.sha256(encode(data)).hexdigest()


def write_json(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        name = Path(stream.name)
        try:
            stream.write(encode(value) + b"\n")
            stream.flush()
            name.chmod(0o644)
            name.replace(path)
        finally:
            name.unlink(missing_ok=True)


def is_message(item, role):
    return item.get("type", "message") == "message" and item.get("role") == role


def is_response_prefix(item):
    return item.get("type") == "reasoning" or is_message(item, "assistant")


def candidates(items, anchor):
    """Find contiguous, unambiguous call batches with their full output batch.

    Adjacent reasoning and assistant text belong to the same response prefix.
    Unknown items break a batch. The newest complete batch stays available.
    """
    ids = collections.Counter(
        (item.get("type"), item.get("call_id"))
        for item in items if item.get("type") in CALLS.keys() | OUTPUTS
    )
    groups = []
    index = anchor + 1
    while index < len(items):
        start = index
        while index < len(items) and is_response_prefix(items[index]):
            index += 1
        call_start = index
        while index < len(items) and items[index].get("type") in CALLS:
            index += 1
        outputs_start = index
        while index < len(items) and items[index].get("type") in OUTPUTS:
            index += 1
        if start == index:
            index += 1
            continue
        calls = items[call_start:outputs_start]
        outputs = items[outputs_start:index]
        expected = collections.Counter((CALLS[call["type"]], call.get("call_id")) for call in calls)
        actual = collections.Counter((output["type"], output.get("call_id")) for output in outputs)
        if calls and expected == actual and all(
            isinstance(call.get("call_id"), str)
            and bool(call["call_id"])
            and ids[(call["type"], call["call_id"])] == 1
            and ids[(CALLS[call["type"]], call["call_id"])] == 1
            for call in calls
        ):
            groups.append((digest(items[start:index])[:24], start, index))
    return {key: (start, end) for key, start, end in groups[:-1]}


def projection(items, groups, dropped, note, anchor):
    omit = {index for key in dropped & groups.keys() for index in range(*groups[key])}
    result = [item for index, item in enumerate(items) if index not in omit]
    if note:
        item = result[anchor]
        content = item["content"]
        if isinstance(content, str):
            content = [{"type": "input_text", "text": content}]
        result[anchor] = {
            **item,
            "content": [*content, {
                "type": "input_text",
                "text": "CLM memory written by the assistant:\n" + note,
            }],
        }
    return result


class Context:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.revision = 0
        self.anchor = None
        self.visible = set()
        self.dropped = set()
        self.note = ""

    def take_edit(self):
        claimed = self.directory / (".claimed-" + uuid.uuid4().hex)
        try:
            (self.directory / "edit.json").replace(claimed)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(os.open(claimed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("invalid_edit")
                data = stream.read(EDIT_LIMIT + 1)
            if len(data) > EDIT_LIMIT:
                raise ValueError("edit_too_large")
            edit = json.loads(data)
            if not isinstance(edit, dict) or set(edit) != {"revision", "drop", "note"}:
                raise ValueError("invalid_edit")
            return edit
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid_edit") from error
        finally:
            if not claimed.is_dir():
                claimed.unlink(missing_ok=True)

    def apply(self, items):
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("expected_full_input_array")
        try:
            anchor_index = next(index for index, item in enumerate(items) if is_message(item, "user"))
        except StopIteration as error:
            raise ValueError("missing_user_anchor") from error
        anchor = digest(items[anchor_index])
        if self.anchor != anchor:
            self.visible.clear()
            self.dropped.clear()
            self.note = ""
            self.anchor = anchor
        groups = candidates(items, anchor_index)
        previous = projection(items, groups, self.dropped, self.note, anchor_index)
        before_bytes = len(encode(items))
        result, status, removed = previous, None, []
        try:
            edit = self.take_edit()
            if edit is not None:
                result, removed = self.apply_edit(edit, items, groups, previous, anchor_index)
                status = "accepted"
        except ValueError as error:
            status = str(error)
        self.revision += 1
        self.visible = set(groups) - self.dropped
        write_json(self.directory / "LIVE_CTX_MAIN.json", {
            "revision": self.revision,
            "last_edit": status,
            "note": self.note,
            "editable": [
                {
                    "id": key,
                    "bytes": len(encode(items[start:end])),
                    "tool_names": [item.get("name") for item in items[start:end] if item.get("type") in CALLS],
                    "items": [item for item in items[start:end] if item.get("type") != "reasoning"],
                }
                for key, (start, end) in groups.items() if key in self.visible
            ],
        })
        return result, {
            "revision": self.revision, "edit": status, "dropped_ids": removed,
            "editable_count": len(self.visible), "removed_items": len(items) - len(result),
            "before_input_bytes": before_bytes, "after_input_bytes": len(encode(result)),
            "raw_input_sha256": digest(items), "upstream_input_sha256": digest(result),
        }

    def apply_edit(self, edit, items, groups, previous, anchor):
        if type(edit["revision"]) is not int or edit["revision"] != self.revision:
            raise ValueError("stale_revision")
        drop, note = edit["drop"], edit["note"]
        if not isinstance(drop, list) or not drop or any(
            not isinstance(key, str) or not EDIT_ID.fullmatch(key) for key in drop
        ):
            raise ValueError("invalid_drop")
        drop = set(drop)
        if not drop <= self.visible & groups.keys():
            raise ValueError("protected_or_unknown_exchange")
        if not isinstance(note, str) or len(note.encode()) > NOTE_LIMIT:
            raise ValueError("invalid_note")
        candidate = projection(items, groups, self.dropped | drop, note, anchor)
        if len(encode(candidate)) >= len(encode(previous)):
            raise ValueError("edit_must_shrink")
        self.dropped.update(drop)
        self.note = note
        return candidate, sorted(drop)
