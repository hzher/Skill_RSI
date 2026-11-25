# Adapted from Skill_RSI_hzh/evidence/archive.py (MIT).
"""Bounded file references, immutable reads and atomic derived writes."""

import gzip
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .schema import encode

_REDACTED_NAMES = {
    "home", "credentials", "auth.json", "settings.local.json", ".git",
    ".credentials.yaml", ".credentials.json", ".aws", ".ssh",
}


def safe_path(root, path):
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    relative = path.relative_to(root)
    candidate = root
    for component in relative.parts:
        lower = component.lower()
        if component == ".." or lower in _REDACTED_NAMES or lower.startswith(".env"):
            raise ValueError(f"Evidence path is outside the allowed scope: {relative}")
        candidate /= component
        if candidate.is_symlink():
            raise ValueError(f"Evidence path traverses a symlink: {relative}")
    return candidate


def signature(path):
    info = path.stat()
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class Archive:
    def __init__(self, root, scope="batch"):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError(f"Expected an evidence directory: {root}")
        self.scope = scope
        self.inventory = {}
        self.diagnostics = []

    def path(self, path):
        return safe_path(self.root, path)

    def ref(self, path, locator=None, layer="native_events"):
        path = self.path(path)
        stat = signature(path)
        if path in self.inventory:
            saved, digest = self.inventory[path]
            if saved != stat:
                raise ValueError(f"Evidence changed during import: {path}")
        else:
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            self.inventory[path] = stat, digest
            self.assert_unchanged(path)
        return {
            "source_root": self.scope, "path": path.relative_to(self.root).as_posix(),
            "locator": locator or {"type": "file"}, "bytes": stat[1], "sha256": digest,
            "payload_layer": layer,
        }

    def assert_unchanged(self, path=None):
        items = [self.path(path)] if path is not None else self.inventory
        for item in items:
            if signature(item) != self.inventory[item][0]:
                raise ValueError(f"Evidence changed during import: {item}")

    def json(self, path):
        path = self.path(path)
        self.ref(path)
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as stream:
            value = json.load(stream)
        self.assert_unchanged(path)
        return value

    def jsonl(self, path, layer="native_events"):
        path = self.path(path)
        ref = self.ref(path, layer=layer)
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rb") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                row_ref = {**ref, "locator": {"type": "line", "line": line_number}}
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("Expected a JSON object")
                except (UnicodeError, ValueError):
                    self.diagnostics.append({
                        "reason": "truncated_record" if not line.endswith(b"\n") else "malformed_record",
                        "raw_ref": row_ref,
                    })
                    continue
                yield row, row_ref, line_number
        self.assert_unchanged(path)


def pointer(path):
    return {"type": "json_pointer", "pointer": path}


def write(path, value, *, lines=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            if lines:
                for row in value:
                    stream.write(encode(row) + b"\n")
            else:
                stream.write(encode(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
