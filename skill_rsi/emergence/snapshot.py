"""Freeze every selected task record without exposing private native content."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from ..components.manifest import canonical, sha256
from ..evidence.io import inside, read_json, source_bytes, write_json
from ..review.projection import PRIVATE_BLOCKS, SECRET

_DROP = object()
_PRIVATE = PRIVATE_BLOCKS | {"native_private_block", "encrypted_content", "reasoning_delta"}
_SECRET_KEY = re.compile(r"(?i)(?:^|[_-])(?:api[_-]?key|access[_-]?key(?:[_-]?id)?|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|session[_-]?token|"
                         r"authorization|password|passwd|client[_-]?secret|secret[_-]?access[_-]?key|private[_-]?key|cookie|credentials?)(?:$|[_-])")
_SECRET_TEXT = re.compile(r"(?i)\b(?:[a-z0-9_]*(?:api_key|access_token|refresh_token|id_token|auth_token|authToken|password|client_secret|secret_access_key))"
                          r"\s*[=:]\s*[\"']?[^\s\"',;}{]{4,}")
_PEM = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)", re.S)
_URL_AUTH = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@", re.I)


def visible(value):
    """Preserve visible structure and text lengths; remove private blocks and secrets."""
    if isinstance(value, dict):
        if any(str(value.get(key, "")).casefold() in _PRIVATE for key in ("type", "role", "kind")):
            return _DROP
        output = {}
        for key, item in value.items():
            if key.casefold() in _PRIVATE or key.casefold() in {"encrypted_content", "signature"}:
                continue
            credential = _SECRET_KEY.search(key) or (key.casefold() in {"token", "secret"} and isinstance(item, str))
            cleaned = "[redacted credential]" if credential else visible(item)
            if cleaned is not _DROP:
                output[key] = cleaned
        return output
    if isinstance(value, list):
        return [cleaned for item in value if (cleaned := visible(item)) is not _DROP]
    if isinstance(value, str):
        text = _PEM.sub("[redacted private key]", value)
        text = _URL_AUTH.sub(r"\1[redacted]@", text)
        return _SECRET_TEXT.sub("[redacted credential]", SECRET.sub("[redacted credential]", text))
    return value


def _captures(task):
    paths = {*task.glob("capture/*.jsonl"), *task.glob("capture/capture/*.jsonl"),
             *task.glob("capture/threads/*/codex-rollout.jsonl")}
    return sorted(path for path in paths if not path.name.endswith(".index.jsonl"))


def freeze(task, output):
    """Write a redacted, byte-indexed snapshot of complete records at opening EOFs."""
    task, output = Path(task), Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    source_task = read_json(inside(task, "task.json"))
    summary = {"schema": "skill-rsi.emergence-snapshot.v1", "source_task_id": source_task["task_id"],
               "record_count": 0, "visible_record_count": 0, "excluded_private_count": 0,
               "stream_count": 0, "coverage_gaps": list(source_task.get("coverage_gaps", [])),
               "capture_policy": source_task.get("capture_policy"), "streams": [], "files": []}
    fields = ("task_id", "host", "source", "title", "project", "status", "created_at", "native_created_at",
              "recording_started_at", "capture_policy", "coverage_gaps", "session_id", "subthreads", "capture_visibility")
    write_json(output / "task.json", visible({key: source_task[key] for key in fields if key in source_task}))
    index_path = output / "source-index.jsonl"
    with index_path.open("xb") as index:
        def record(raw, reference, stream, target, target_line, target_name):
            summary["record_count"] += 1
            entry = {"stream": stream, "source": reference, "snapshot": None}
            try:
                value = json.loads(raw)
                projected = visible(value)
            except (ValueError, UnicodeError):
                entry["status"] = "invalid_json"
                summary["coverage_gaps"].append("unparseable_source_record")
            else:
                if projected is _DROP:
                    entry["status"] = "excluded_private"
                    summary["excluded_private_count"] += 1
                else:
                    # A wrapper containing only private content carries no text
                    # after projection, but its visible boundary remains useful.
                    data = canonical(projected) + b"\n"
                    target.write(data)
                    target_line += 1
                    summary["visible_record_count"] += 1
                    entry.update(status="visible", snapshot={"path": target_name,
                                 "line": target_line, "sha256": sha256(data), "bytes": len(data)})
            index.write(canonical(entry) + b"\n")
            return target_line

        for path in _captures(task):
            relative = path.relative_to(task).as_posix()
            path = inside(task, relative)
            if not path.is_file():
                raise ValueError("轨迹来源必须是普通文件：" + relative)
            target_path = output / "streams" / relative
            target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            size, count, target_line, consumed = path.stat().st_size, 0, 0, 0
            prefix = hashlib.sha256()
            with path.open("rb") as source, target_path.open("xb") as target:
                while consumed < size:
                    raw = source.readline(size - consumed)
                    if not raw:
                        raise ValueError("A capture source changed while freezing its snapshot")
                    reference = {"path": relative, "line": count + 1, "byte_offset": consumed,
                                 "bytes": len(raw), "sha256": sha256(raw)}
                    prefix.update(raw)
                    consumed += len(raw)
                    if not raw.endswith(b"\n"):
                        index.write(canonical({"stream": relative, "source": reference, "snapshot": None,
                                               "status": "partial"}) + b"\n")
                        summary["coverage_gaps"].append("source_partial_record_at_snapshot")
                        break
                    count += 1
                    target_line = record(raw, reference, relative, target, target_line, target_path.relative_to(output).as_posix())
                source.seek(0)
                check = hashlib.sha256()
                left = size
                while left:
                    chunk = source.read(min(left, 1024 * 1024))
                    if not chunk:
                        raise ValueError("A capture source was truncated while freezing its snapshot")
                    left -= len(chunk)
                    check.update(chunk)
                if check.digest() != prefix.digest():
                    raise ValueError("A capture source was rewritten while freezing its snapshot")
            summary["streams"].append({"path": relative, "source_bytes": size, "source_sha256": prefix.hexdigest(),
                                        "record_count": count, "visible_record_count": target_line})
        database = inside(task, "journal/journal.sqlite3")
        if database.is_file():
            target_path = output / "streams/journal/events.jsonl"
            target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            count = target_line = 0
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection, target_path.open("xb") as target:
                connection.execute("BEGIN")
                for ordinal, payload in connection.execute("SELECT ordinal,payload FROM events ORDER BY ordinal"):
                    event = json.loads(payload)
                    ref = event["raw_ref"]
                    raw = source_bytes(task / "journal", ref)
                    reference = {"path": "journal/" + ref["path"], "sha256": ref["sha256"], "bytes": len(raw),
                                 "locator": ref.get("locator"), "ordinal": ordinal}
                    count += 1
                    target_line = record(raw, reference, "journal/events", target, target_line, target_path.relative_to(output).as_posix())
            summary["streams"].append({"path": "journal/events", "record_count": count, "visible_record_count": target_line})
    summary["stream_count"] = len(summary["streams"])
    summary["coverage_gaps"] = sorted(set(summary["coverage_gaps"]))
    if not summary["visible_record_count"]:
        raise ValueError("这个任务没有可供生成器读取的可见轨迹")
    summary["files"] = fingerprints(output)
    write_json(output / "manifest.json", summary)
    return summary


def fingerprints(root):
    return [{"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
             "sha256": _file_sha(path)} for path in sorted(Path(root).rglob("*")) if path.is_file()]


def _file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify(root, files):
    expected = {item["path"] for item in files}
    actual = set()
    for path in Path(root).rglob("*"):
        if path.is_symlink():
            raise ValueError("冻结的生成器输入包含符号链接")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != expected:
        raise ValueError("冻结的生成器输入文件集合已被修改")
    for item in files:
        path = inside(root, item["path"])
        if not path.is_file() or path.stat().st_size != item["bytes"] or _file_sha(path) != item["sha256"]:
            raise ValueError("冻结的生成器输入已被修改：" + item["path"])
