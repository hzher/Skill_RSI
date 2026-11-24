"""Standard local JSON, JSONL, and NDJSON sources."""

import hashlib
import json
from pathlib import Path

from ..evidence.io import digest
from ..evidence.schema import identifier as stable_id
from .base import Origin


class LocalFileSource:
    def __init__(self, path, *, name=None, follow=False):
        self.path = Path(path)
        self.name = name or self.path.name
        self.follow = follow
        if self.path.suffix not in {".json", ".jsonl", ".ndjson"}:
            raise ValueError(f"Unsupported data file: {self.path}")

    def read(self, cursor=None):
        if self.path.suffix == ".json":
            yield from self._document(cursor)
        else:
            yield from self._lines(cursor)

    def _document(self, cursor):
        raw = self.path.read_bytes()
        fingerprint = digest(raw)
        doc = json.loads(raw)
        if isinstance(doc, list):
            records, prefix = doc, ""
        elif isinstance(doc, dict):
            records, prefix = [doc], None
            if isinstance(doc.get("records"), list):
                records, prefix = doc["records"], "/records"
        else:
            raise ValueError(f"Expected an object or record array: {self.path}")
        start = cursor["items"] if cursor and cursor.get("prefix_sha256") == fingerprint else 0
        generation = stable_id(self.name, fingerprint)
        for index in range(start, len(records)):
            entry = records[index]
            if not isinstance(entry, dict):
                raise ValueError(f"Expected object at record index {index} in {self.path}")
            locator = {"type": "file"} if prefix is None else {
                "type": "json_pointer", "pointer": f"{prefix}/{index}",
            }
            origin = Origin(self.name, generation, locator, raw)
            yield entry, origin, {
                "generation": generation, "items": index + 1, "prefix_sha256": fingerprint,
                "close_status": "complete" if index + 1 == len(records) else "open",
            }

    def _lines(self, cursor):
        with self.path.open("rb") as stream:
            generation, offset, line_no, hasher, rotated = self._resume(stream, cursor)
            while raw := stream.readline():
                if self.follow and not raw.endswith(b"\n"):
                    break
                end = stream.tell()
                line_no += 1
                hasher.update(raw)
                origin = Origin(self.name, generation, {"type": "line", "line": line_no},
                                raw, "jsonl", offset, end)
                next_cursor = {
                    "generation": generation, "line": line_no, "offset": end,
                    "prefix_sha256": hasher.hexdigest(), "rotation": rotated,
                    "close_status": "open" if self.follow else "snapshot",
                    "terminated": raw.endswith(b"\n"),
                }
                offset = end
                rotated = False
                if not raw.strip():
                    next_cursor["skip_reason"] = "blank"
                    yield None, origin, next_cursor
                    continue
                try:
                    row = json.loads(raw)
                    if not isinstance(row, dict):
                        raise ValueError("Expected a JSON object")
                except (UnicodeError, ValueError):
                    next_cursor["skip_reason"] = "malformed_record"
                    row = None
                yield row, origin, next_cursor

    def _resume(self, stream, cursor):
        hasher = hashlib.sha256()
        generation = stable_id(self.name, "first")
        if cursor is None:
            return generation, 0, 0, hasher, False
        remaining = cursor["offset"]
        while remaining > 0 and (block := stream.read(min(remaining, 256 * 1024))):
            remaining -= len(block)
            hasher.update(block)
        if remaining == 0 and hasher.hexdigest() == cursor["prefix_sha256"]:
            if cursor.get("terminated", True):
                return cursor["generation"], cursor["offset"], cursor["line"], hasher, False
            suffix = stream.read(1)
            if suffix in {b"", b"\n"}:
                hasher.update(suffix)
                return cursor["generation"], stream.tell(), cursor["line"], hasher, False
        stream.seek(0)
        prefix = stream.read(4096)
        stream.seek(0)
        next_generation = stable_id(self.name, cursor["generation"], digest(prefix))
        return next_generation, 0, 0, hashlib.sha256(), True
