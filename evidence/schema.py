"""Shared protocol records used by the native readers."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

SCHEMA = "SkillRSI-CTX-v1"
GENERATOR = "1.0.0"


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def identifier(*parts):
    return hashlib.sha256(encode(parts)).hexdigest()[:24]


def utc(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
        return datetime.fromtimestamp(value / (1000 if abs(value) >= 10**11 else 1), timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def event(raw_ref, type, seq, **fields):
    source = raw_ref["path"]
    actor = fields.get("actor_type")
    stamp = fields.get("timestamp")
    return {
        "event_id": identifier(source, raw_ref.get("locator"), seq, type),
        "source_id": source, "seq": seq, "type": type, "raw_ref": raw_ref,
        "instance_id": None, "instance_index": None, "actor_type": actor,
        "actor_id": fields.get("native_session_id") or fields.get("thread_id") if actor == "agent" else actor,
        "native_session_id": None, "thread_id": None, "parent_event_id": None,
        "request_id": None, "label_status": "not_applicable", **fields,
        "native_timestamp": stamp, "timestamp": utc(stamp),
    }


@dataclass
class NativeRun:
    identity: dict
    events: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    versions: list = field(default_factory=list)
    scores: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)


def references(value):
    if isinstance(value, dict):
        if {"source_root", "path", "locator", "sha256", "bytes"} <= value.keys():
            yield value
        else:
            for item in value.values():
                yield from references(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from references(item)
