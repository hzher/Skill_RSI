"""Incrementally preserve authorized native Codex CLI and Desktop rollout records."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from bisect import bisect_right
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from ...components._local import state_root
from ...evidence.io import at_pointer, file_lock, inside, read_json, write_bytes, write_json
from ...tasks.store import TaskStore, _edit, managed_session_task, session_task

_LINE_LIMIT = 32 * 1024 * 1024
_BATCH = 500
_GLOBAL = "*"
_META_KEYS = {"id", "cwd", "source", "originator", "thread_source", "timestamp", "parent_thread_id", "forked_from_id"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    path TEXT PRIMARY KEY, thread TEXT NOT NULL, inode TEXT NOT NULL,
    offset INTEGER NOT NULL, line INTEGER NOT NULL, prefix_sha TEXT NOT NULL,
    size INTEGER NOT NULL, mtime INTEGER NOT NULL,
    epoch TEXT NOT NULL DEFAULT '', skip_partial INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS threads (
    thread TEXT PRIMARY KEY, project TEXT NOT NULL, task_id TEXT NOT NULL, task_path TEXT NOT NULL,
    published INTEGER NOT NULL DEFAULT 0, log_bytes INTEGER NOT NULL DEFAULT 0,
    index_bytes INTEGER NOT NULL DEFAULT 0, root_thread TEXT, capture_subdir TEXT NOT NULL DEFAULT 'capture',
    task_mode TEXT NOT NULL DEFAULT 'observed'
);
CREATE TABLE IF NOT EXISTS records (
    thread TEXT NOT NULL, line INTEGER NOT NULL, sha TEXT NOT NULL, seq INTEGER NOT NULL,
    bytes INTEGER NOT NULL, origin TEXT NOT NULL,
    PRIMARY KEY (thread, line, sha), UNIQUE (thread, seq)
);
CREATE TABLE IF NOT EXISTS session_metadata (
    thread TEXT PRIMARY KEY, project TEXT NOT NULL, metadata TEXT NOT NULL, path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS baselines (
    project TEXT NOT NULL,path TEXT NOT NULL,inode TEXT NOT NULL,offset INTEGER NOT NULL,
    partial INTEGER NOT NULL,epoch TEXT NOT NULL,prefix_sha TEXT NOT NULL DEFAULT '',PRIMARY KEY(project,path)
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _configuration(state):
    path = state / "codex-recording.json"
    return read_json(path) if path.is_file() else {"projects": {}, "codex_home": str(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve())}


def _baseline(connection, sources, key, epoch):
    for source in sources:
        try:
            stat = source.stat()
            if source.is_symlink():
                continue
            with source.open("rb") as stream:
                size, _, digest, partial = _snapshot_position(stream)
            connection.execute("INSERT OR REPLACE INTO baselines VALUES(?,?,?,?,?,?,?)",
                               (key, str(source), f"{stat.st_dev}:{stat.st_ino}", size, partial, epoch, digest.hexdigest()))
        except OSError:
            continue


def configure(project=None, state_dir=None, *, enabled=True, codex_home=None, tasks_dir=None,
              capture_existing=False, scope=None):
    if type(enabled) is not bool:
        raise ValueError("enabled must be true or false")
    if type(capture_existing) is not bool:
        raise ValueError("capture_existing must be true or false")
    explicit_scope = scope
    scope = scope or ("all_projects" if project is None else "project")
    scope = "project" if scope == "projects" else scope
    if scope not in {"all_projects", "project"}:
        raise ValueError("scope must be all_projects or project")
    if scope == "all_projects" and capture_existing:
        raise ValueError("All-project recording only captures new data; use import_session for authorized history")
    if project is not None:
        project = Path(project).expanduser().resolve(strict=True)
        if not project.is_dir():
            raise ValueError("The recording project must be a directory")
    elif scope != "all_projects":
        raise ValueError("A project is required for project recording")
    state = state_root(state_dir)
    with file_lock(state / ".codex-recording.lock"):
        config = _configuration(state)
        if codex_home is not None:
            config["codex_home"] = str(Path(codex_home).expanduser().resolve())
        prior = config.get("global", {}) if scope == "all_projects" else config["projects"].get(str(project), {})
        old_scope = config.get("scope", "project")
        old_scope = "project" if old_scope == "projects" else old_scope
        switching = (scope == "all_projects" or explicit_scope is not None) and old_scope != scope
        fresh = enabled and (switching or not prior.get("enabled") or "recording_started_at" not in prior)
        setting = {**prior, "enabled": enabled, "configured_at": _now(), "capture_existing": capture_existing}
        if fresh:
            setting["recording_started_at"] = _now()
        if scope == "all_projects":
            config["global"] = setting
            config["scope"] = scope
        else:
            config["projects"][str(project)] = setting
            if explicit_scope:
                config["scope"] = scope
            else:
                config.setdefault("scope", scope)
        if fresh and not capture_existing:
            with closing(_connect(state)) as connection, connection:
                _baseline(connection, _sources(Path(config["codex_home"])),
                          _GLOBAL if scope == "all_projects" else str(project), setting["recording_started_at"])
        write_json(state / "codex-recording.json", config)
        TaskStore(tasks_dir, state_dir=state)
    return status(state, project=project)


def _connect(state):
    path = state / "codex-recording.sqlite3"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    for table, columns in {"streams": {"epoch": "TEXT NOT NULL DEFAULT ''", "skip_partial": "INTEGER NOT NULL DEFAULT 0"},
                           "threads": {"root_thread": "TEXT", "capture_subdir": "TEXT NOT NULL DEFAULT 'capture'",
                                       "task_mode": "TEXT NOT NULL DEFAULT 'observed'"},
                           "baselines": {"prefix_sha": "TEXT NOT NULL DEFAULT ''"}}.items():
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    return connection


def _sources(home):
    return sorted({source for directory in ("sessions", "archived_sessions")
                   for source in (home / directory).rglob("*.jsonl")})


def _inventory(connection, sources):
    index = {}
    for source in sources:
        if source.is_symlink():
            continue
        try:
            with source.open("rb") as stream:
                raw = stream.readline(_LINE_LIMIT + 1)
            if not raw.endswith(b"\n"):
                continue
            metadata, project = _metadata(raw)
            minimal = {key: value for key, value in metadata.items() if key in _META_KEYS}
            connection.execute("INSERT OR REPLACE INTO session_metadata VALUES(?,?,?,?)",
                               (metadata["id"], project, json.dumps(minimal), str(source)))
            index[metadata["id"]] = {"metadata": minimal, "project": project, "path": str(source)}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return index


def _root(metadata, index):
    seen = set()
    while True:
        thread = metadata["id"]
        if thread in seen:
            return None
        seen.add(thread)
        source = metadata.get("source")
        thread_source = metadata.get("thread_source")
        child = (isinstance(thread_source, str) and thread_source in {"subagent", "guardian_review"}) or (isinstance(source, dict) and "subagent" in source)
        if metadata.get("thread_source") == "user" or not child:
            return index.get(thread)
        subagent = source.get("subagent", {}) if isinstance(source, dict) else {}
        spawned = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}
        parent = metadata.get("parent_thread_id") or spawned.get("parent_thread_id")
        if not isinstance(parent, str) or parent not in index:
            return None
        metadata = index[parent]["metadata"]


def status(state_dir=None, *, project=None):
    state = state_root(state_dir)
    config = _configuration(state)
    selected = str(Path(project).expanduser().resolve()) if project is not None else None
    home = Path(config["codex_home"])
    scope = config.get("scope", "project")
    scope = "project" if scope == "projects" else scope
    global_setting = config.get("global", {})
    global_mode = scope == "all_projects"
    report = config.get("sync_reports", {}).get(selected, {}) if selected and not global_mode else config.get("last_sync", {})
    enabled = global_setting.get("enabled", False) if global_mode else config["projects"].get(selected, {}).get("enabled", False) if selected else any(row["enabled"] for row in config["projects"].values())
    database = state / "codex-recording.sqlite3"
    threads = 0
    initializing = False
    if database.is_file():
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
                if selected and not global_mode:
                    threads = connection.execute("SELECT count(DISTINCT task_id) FROM threads WHERE project=? AND task_mode<>'llm_analysis'", (selected,)).fetchone()[0]
                else:
                    threads = connection.execute("SELECT count(DISTINCT task_id) FROM threads WHERE task_mode<>'llm_analysis'").fetchone()[0]
        except sqlite3.Error:
            initializing = True
    source_present = any((home / directory).is_dir() for directory in ("sessions", "archived_sessions"))
    phase = "disabled" if not enabled else "missing_source" if not source_present else "initializing" if initializing else report.get("phase", "ready")
    errors = report.get("errors", [])
    return {"enabled": enabled, "project": selected, "projects": config["projects"], "codex_home": str(home),
            "scope": scope, "all_projects": global_mode, "global": global_setting,
            "source_present": source_present, "phase": phase, "status": phase, "threads": threads,
            "recording_started_at": global_setting.get("recording_started_at") if global_mode else config["projects"].get(selected, {}).get("recording_started_at") if selected else None,
            "session_count": threads, "last_sync": report.get("synced_at"), "last_sync_report": report,
            "last_error": errors[0]["error"] if errors else None,
            "visibility": {"native_rollout": "observed" if threads else "unknown", "physical_model_requests": "unknown"}}


def _metadata(raw):
    row = json.loads(raw)
    body = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(row, dict) or row.get("type") != "session_meta" or not isinstance(body, dict):
        raise ValueError("The first complete rollout record must be session_meta")
    if not isinstance(body.get("id"), str) or not body["id"] or not isinstance(body.get("cwd"), str):
        raise ValueError("session_meta requires thread id and cwd")
    if not Path(body["cwd"]).is_absolute():
        raise ValueError("session_meta cwd must be absolute")
    body = dict(body)
    body.setdefault("timestamp", row.get("timestamp"))
    return body, str(Path(body["cwd"]).expanduser().resolve())


def _source(metadata):
    originator = str(metadata.get("originator") or "").lower()
    source = metadata.get("source")
    # A CLI launched from the desktop can inherit its originator override.
    # The native invocation source still identifies exec and terminal sessions.
    if isinstance(source, str) and source in {"cli", "exec"}:
        return "cli"
    if "desktop" in originator:
        return "desktop"
    if originator == "codex_cli_rs":
        return "cli"
    return "unknown"


def _task(connection, metadata, project, state, root, setting, home):
    thread = metadata["id"]
    existing = connection.execute("SELECT * FROM threads WHERE thread=?", (thread,)).fetchone()
    if existing:
        if existing["project"] != project or existing["root_thread"] != root["metadata"]["id"]:
            raise ValueError("The native thread changed its authorized project or root conversation identity")
        if not (Path(existing["task_path"]) / "task.json").is_file():
            raise ValueError("The indexed task folder is missing")
        TaskStore(state_dir=state).path(existing["task_id"])
        return dict(existing)
    root_metadata = root["metadata"]
    root_id = root_metadata["id"]
    managed, pending = managed_session_task(project, "codex", root_id, state_dir=state)
    if managed is None and pending:
        return None
    title = _title(home, root_id)
    if not title:
        try:
            with Path(root["path"]).open("rb") as stream:
                title = _user_title(stream)
        except OSError:
            pass
    if not managed and not title and not setting.get("capture_existing", False):
        # Native context can arrive before the first user message or sidebar name.
        # Retry at the same event rather than creating a permanently anonymous folder.
        return None
    task = managed or session_task(project, "codex", root_id, state_dir=state,
                                   source=_source(root_metadata), title=title or "Codex 会话",
                                   created_at=_now())
    safe_thread = thread if len(thread) <= 96 and all(char.isascii() and (char.isalnum() or char in "-_.") for char in thread) and thread not in {".", ".."} else hashlib.sha256(thread.encode()).hexdigest()
    subdir = "capture" if thread == root_id else "capture/threads/" + safe_thread
    connection.execute("INSERT INTO threads(thread,project,task_id,task_path,root_thread,capture_subdir,task_mode) VALUES(?,?,?,?,?,?,?)",
                       (thread, project, task["task_id"], task["path"], root_id, subdir, task["mode"]))
    return dict(connection.execute("SELECT * FROM threads WHERE thread=?", (thread,)).fetchone())


def _title(home, thread):
    database = home / "state_5.sqlite"
    if not database.is_file():
        return None
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
            for column in ("name", "title"):
                try:
                    row = connection.execute(f"SELECT {column} FROM threads WHERE id=?", (thread,)).fetchone()
                except sqlite3.Error:
                    continue
                if row and isinstance(row[0], str) and row[0].strip():
                    return row[0].strip().splitlines()[0][:160]
    except sqlite3.Error:
        return None


def _refresh_title(task, home, root_id):
    title = _title(home, root_id)
    if not title:
        return
    path = Path(task["task_path"])
    current = read_json(path / "task.json")
    changes = {"native_title": title, "session_title": title}
    if current.get("mode") == "observed":
        changes["title"] = title
    if any(current.get(key) != value for key, value in changes.items()):
        with _edit(path) as record:
            record.update(changes)


def _user_title(stream):
    from ..native_events import native_parts

    position = stream.tell()
    stream.seek(0)
    try:
        for _ in range(64):
            raw = stream.readline(_LINE_LIMIT + 1)
            if not raw.endswith(b"\n"):
                break
            try:
                row = json.loads(raw)
                if not isinstance(row, dict):
                    continue
                for kind, pointer, _ in native_parts(row, "codex"):
                    if kind == "user_message":
                        text = at_pointer(row, pointer)
                        if isinstance(text, str) and text.strip():
                            return text.strip().splitlines()[0][:80]
            except (ValueError, KeyError, TypeError):
                continue
    finally:
        stream.seek(position)
    return None


def _record(connection, task, number, raw, source, offset, *, provenance=None):
    sha = hashlib.sha256(raw).hexdigest()
    duplicate = connection.execute("SELECT 1 FROM records WHERE thread=? AND line=? AND sha=?",
                                   (task["thread"], number, sha)).fetchone()
    if duplicate:
        return 0
    object_path = inside(Path(task["task_path"]), task["capture_subdir"] + "/native-codex/" + sha + ".jsonl")
    if not object_path.exists():
        write_bytes(object_path, raw, replace=False)
    elif object_path.is_symlink() or object_path.read_bytes() != raw:
        raise ValueError("Native rollout object differs from its content digest")
    seq = connection.execute("SELECT coalesce(max(seq),0)+1 FROM records WHERE thread=?", (task["thread"],)).fetchone()[0]
    origin = {"source_path": str(source), "line": number, "byte_offset": offset,
              "sha256": sha, "bytes": len(raw), "record_path": "native-codex/" + sha + ".jsonl"}
    if provenance:
        origin.update(provenance)
    connection.execute("INSERT INTO records VALUES(?,?,?,?,?,?)", (task["thread"], number, sha, seq, len(raw), json.dumps(origin)))
    return 1


def _publish(connection, task):
    current = connection.execute("SELECT * FROM threads WHERE thread=?", (task["thread"],)).fetchone()
    capture = inside(Path(task["task_path"]), task["capture_subdir"])
    capture.mkdir(mode=0o700, parents=True, exist_ok=True)
    log = inside(Path(task["task_path"]), task["capture_subdir"] + "/codex-rollout.jsonl")
    index = inside(Path(task["task_path"]), task["capture_subdir"] + "/codex-rollout.index.jsonl")
    published, log_bytes, index_bytes = current["published"], current["log_bytes"], current["index_bytes"]
    if (log.exists() and log.stat().st_size < log_bytes) or (index.exists() and index.stat().st_size < index_bytes) or not log.exists() or not index.exists():
        published = log_bytes = index_bytes = 0
    if log.is_symlink() or index.is_symlink():
        raise ValueError("The rollout view must be an ordinary file")
    rows = connection.execute("SELECT seq,sha,bytes,origin FROM records WHERE thread=? AND seq>? ORDER BY seq", (task["thread"], published))
    with log.open("a+b") as output, index.open("a+b") as origins:
        os.fchmod(output.fileno(), 0o600)
        os.fchmod(origins.fileno(), 0o600)
        output.truncate(log_bytes)
        origins.truncate(index_bytes)
        output.seek(log_bytes)
        origins.seek(index_bytes)
        for row in rows:
            raw = inside(Path(task["task_path"]), task["capture_subdir"] + "/native-codex/" + row["sha"] + ".jsonl").read_bytes()
            if len(raw) != row["bytes"] or hashlib.sha256(raw).hexdigest() != row["sha"]:
                raise ValueError("Native rollout source object is incomplete")
            output.write(raw)
            origins.write((row["origin"] + "\n").encode())
            published = row["seq"]
        output.flush()
        origins.flush()
        os.fsync(output.fileno())
        os.fsync(origins.fileno())
        connection.execute("UPDATE threads SET published=?,log_bytes=?,index_bytes=? WHERE thread=?",
                           (published, output.tell(), origins.tell(), task["thread"]))


def _order_import(connection, task, native_order):
    """Restore source order when history is added after live rows were published."""
    rows = connection.execute("SELECT line,sha FROM records WHERE thread=? ORDER BY seq", (task["thread"],)).fetchall()
    current = [(row["line"], row["sha"]) for row in rows]
    native = set(native_order)
    ordered = [key for key in current if key not in native] + native_order
    if current == ordered:
        return
    connection.execute("UPDATE records SET seq=-seq WHERE thread=?", (task["thread"],))
    for seq, (line, sha) in enumerate(ordered, 1):
        connection.execute("UPDATE records SET seq=? WHERE thread=? AND line=? AND sha=?", (seq, task["thread"], line, sha))
    connection.execute("UPDATE threads SET published=0,log_bytes=0,index_bytes=0 WHERE thread=?", (task["thread"],))


def _position(stream, length):
    stream.seek(0)
    digest = hashlib.sha256()
    count = 0
    left = length
    while left:
        raw = stream.read(min(left, 1024 * 1024))
        if not raw:
            return None
        digest.update(raw)
        count += raw.count(b"\n")
        left -= len(raw)
    return digest, count


def _snapshot_position(stream):
    size = os.fstat(stream.fileno()).st_size
    state = _position(stream, size)
    if state is None:
        raise ValueError("The native source changed while establishing its baseline")
    stream.seek(max(0, size - 1))
    partial = size > 0 and stream.read(1) != b"\n"
    stream.seek(size)
    return size, state[1], state[0], int(partial)


def _task_metadata(connection, task, root, metadata, source, setting, index):
    root_metadata = root["metadata"]
    context = {"context_only": True, "recording_started_at": setting.get("recording_started_at"),
               "native_session_meta": {key: value for key, value in metadata.items() if key in _META_KEYS},
               "source_path": str(source)}
    context_path = inside(Path(task["task_path"]), task["capture_subdir"] + "/session-meta.json")
    if not context_path.exists():
        write_json(context_path, context)
    root_context = inside(Path(task["task_path"]), "capture/session-meta.json")
    if not root_context.exists():
        write_json(root_context, {**context, "native_session_meta": root_metadata, "source_path": root["path"]})
    children = []
    for row in connection.execute("SELECT thread,capture_subdir FROM threads WHERE task_id=? AND thread<>?",
                                  (task["task_id"], root_metadata["id"])):
        child = index.get(row["thread"], {})
        child_meta = child.get("metadata", {})
        children.append({"thread_id": row["thread"], "cwd": child.get("project"),
                         "source": child_meta.get("source"), "parent_thread_id": child_meta.get("parent_thread_id"),
                         "path": row["capture_subdir"] + "/codex-rollout.jsonl"})
    with _edit(Path(task["task_path"])) as record:
        imported = setting.get("capture_policy") == "targeted_backfill" or record.get("capture_policy") == "targeted_backfill"
        record.update(session_id=root_metadata["id"], native_source=root_metadata.get("source"),
                      originator=root_metadata.get("originator"), forked_from_id=root_metadata.get("forked_from_id"),
                      parent_thread_id=root_metadata.get("parent_thread_id"), native_created_at=root_metadata.get("timestamp"),
                      recording_started_at=setting.get("recording_started_at"),
                      capture_policy="targeted_backfill" if imported else "backfill" if setting.get("capture_existing") else "new_only",
                      skipped_history=False if imported else not setting.get("capture_existing", False),
                      coverage_gaps=[] if imported or setting.get("capture_existing") else ["history_before_recording_not_captured"],
                      native_rollout="capture/codex-rollout.jsonl", subthread_count=len(children), subthreads=children,
                      native_record_count=connection.execute("SELECT count(*) FROM records WHERE thread IN (SELECT thread FROM threads WHERE task_id=?)", (task["task_id"],)).fetchone()[0],
                      native_source_paths=sorted({*record.get("native_source_paths", []), str(source)}),
                      capture_visibility={"native_rollout": "observed", "physical_model_requests": "unknown"})
        if setting.get("capture_policy") == "targeted_backfill":
            record.setdefault("imported_at", setting["imported_at"])
            record.update(last_imported_at=setting["imported_at"], authorized_root_thread=root_metadata["id"],
                          authorized_import_threads=setting["authorized_import_threads"],
                          import_scope="explicit_session_family", capture_policy_after_import="new_only")


def _scan_file(connection, source, allowed, state, home, budget, *, metadata_index=None, policies=None):
    current = home
    for part in source.relative_to(home).parts:
        current /= part
        if current.is_symlink():
            return None, 0, False
    index = metadata_index or {}
    policies = policies or _configuration(state)["projects"]
    with source.open("rb") as stream:
        header = stream.readline(_LINE_LIMIT + 1)
        if not header.endswith(b"\n"):
            return None, 0, False
        metadata, actual_project = _metadata(header)
        if metadata["id"] not in index:
            index = {**index, metadata["id"]: {"metadata": metadata, "project": actual_project, "path": str(source)}}
        root = _root(metadata, index)
        if root is None or root["project"] not in allowed:
            return None, 0, False
        project = root["project"]
        setting = policies[project]
        new_only = not setting.get("capture_existing", False)
        epoch = setting.get("recording_started_at", "")
        stat = os.fstat(stream.fileno())
        inode = f"{stat.st_dev}:{stat.st_ino}"
        previous = connection.execute("SELECT * FROM streams WHERE path=? AND thread=? AND epoch=?",
                                      (str(source), metadata["id"], epoch)).fetchone()
        if previous is None:
            previous = connection.execute("SELECT * FROM streams WHERE thread=? AND epoch=? AND inode=? ORDER BY offset DESC LIMIT 1",
                                          (metadata["id"], epoch, inode)).fetchone()
        known = connection.execute("SELECT * FROM threads WHERE thread=?", (metadata["id"],)).fetchone()
        if known and (known["project"] != project or known["root_thread"] != root["metadata"]["id"]):
            raise ValueError("The native thread changed its project or root conversation identity")
        task = dict(known) if known else None
        if previous and previous["inode"] == inode and previous["size"] == stat.st_size and previous["mtime"] == stat.st_mtime_ns and previous["offset"] == stat.st_size:
            if task:
                TaskStore(state_dir=state).path(task["task_id"])
                capture = Path(task["task_path"]) / task["capture_subdir"]
                log, origins = capture / "codex-rollout.jsonl", capture / "codex-rollout.index.jsonl"
                if not log.is_file() or not origins.is_file() or log.stat().st_size != task["log_bytes"] or origins.stat().st_size != task["index_bytes"]:
                    _publish(connection, task)
                if metadata["id"] == root["metadata"]["id"]:
                    _refresh_title(task, home, root["metadata"]["id"])
            return task, 0, False
        offset = number = skip_partial = 0
        digest = hashlib.sha256()
        reset = previous is not None
        if previous and stat.st_size >= previous["offset"]:
            checked = _position(stream, previous["offset"])
            if checked is not None and checked[0].hexdigest() == previous["prefix_sha"]:
                offset, number, digest = previous["offset"], previous["line"], checked[0]
                skip_partial = previous["skip_partial"]
                reset = False
        if new_only and previous is None:
            baseline = connection.execute("SELECT * FROM baselines WHERE project=? AND epoch=? AND (path=? OR inode=?) ORDER BY offset DESC LIMIT 1",
                                          (setting.get("baseline_key", project), epoch, str(source), inode)).fetchone()
            if baseline and baseline["inode"] == inode and stat.st_size >= baseline["offset"]:
                checked = _position(stream, baseline["offset"])
                if checked is not None and checked[0].hexdigest() == baseline["prefix_sha"]:
                    offset, number, digest = baseline["offset"], checked[1], checked[0]
                    skip_partial = baseline["partial"]
                else:
                    reset = True
            elif baseline:
                reset = True
            else:
                # An old session entering the watched directories gets a fresh EOF
                # boundary. Context timestamps never become recorded task events.
                stamp = metadata.get("timestamp")
                try:
                    reset = (datetime.fromisoformat(stamp.replace("Z", "+00:00")) < datetime.fromisoformat(epoch)) if isinstance(stamp, str) and epoch else (stat.st_mtime < datetime.fromisoformat(epoch).timestamp() if epoch else True)
                except (TypeError, ValueError):
                    reset = stat.st_mtime < datetime.fromisoformat(epoch).timestamp() if epoch else True
        if new_only and reset:
            offset, number, digest, skip_partial = _snapshot_position(stream)
        stream.seek(offset)
        added = 0
        pending = False
        for _ in range(budget):
            raw = stream.readline(_LINE_LIMIT + 1)
            if not raw:
                break
            if len(raw) > _LINE_LIMIT:
                raise ValueError("A native rollout line exceeds the 32 MiB capture limit")
            if skip_partial:
                digest.update(raw)
                offset += len(raw)
                if raw.endswith(b"\n"):
                    number += 1
                    skip_partial = 0
                    continue
                pending = True
                break
            if not raw.endswith(b"\n"):
                pending = True
                break
            number += 1
            old_timestamp = False
            if new_only and epoch:
                try:
                    row = json.loads(raw)
                    stamp = row.get("timestamp") if isinstance(row, dict) else None
                    old_timestamp = isinstance(stamp, str) and datetime.fromisoformat(stamp.replace("Z", "+00:00")) < datetime.fromisoformat(epoch)
                except (ValueError, TypeError):
                    pass
            if not (new_only and number == 1) and not old_timestamp:
                if task is None:
                    task = _task(connection, metadata, project, state, root, setting, home)
                    if task is None:
                        number -= 1
                        pending = True
                        break
                added += _record(connection, task, number, raw, source, offset)
            digest.update(raw)
            offset += len(raw)
        else:
            pending = offset < os.fstat(stream.fileno()).st_size
        connection.execute("INSERT OR REPLACE INTO streams VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (str(source), metadata["id"], inode, offset, number, digest.hexdigest(), stat.st_size,
                            stat.st_mtime_ns, epoch, skip_partial))
        if task:
            _publish(connection, task)
            _task_metadata(connection, task, root, metadata, source, setting, index)
            _refresh_title(task, home, root["metadata"]["id"])
        return task, added, pending


def import_session(thread_id, state_dir=None):
    """Import only an explicitly selected visible chat and its proven descendants.

    This does not enable historical collection or change recording configuration.
    Each source is bounded at its opening EOF; future rows follow normal new-only
    recording. User forks remain separate even when their history shares a prefix.
    """
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("A native root thread id is required")
    state = state_root(state_dir)
    with file_lock(state / ".codex-recording.lock"):
        config = _configuration(state)
        home = Path(config["codex_home"])
        imported_at = _now()
        result = {"thread_id": thread_id, "family": [], "records": 0, "errors": [],
                  "imported_at": imported_at, "capture_policy": "targeted_backfill"}
        with closing(_connect(state)) as connection:
            with connection:
                index = _inventory(connection, _sources(home))
            root = index.get(thread_id)
            resolved = _root(root["metadata"], index) if root else None
            if resolved is None or resolved["metadata"]["id"] != thread_id:
                raise ValueError("Select an existing visible root chat, not a subagent or unresolved parent")
            family = {thread: row for thread, row in index.items()
                      if (ancestor := _root(row["metadata"], index)) is not None
                      and ancestor["metadata"]["id"] == thread_id}
            result["family"] = [thread_id, *sorted(set(family) - {thread_id})]
            active = config.get("global", {}) if config.get("scope") == "all_projects" else config["projects"].get(root["project"], {})
            epoch = active.get("recording_started_at", imported_at)
            setting = {"capture_existing": True, "capture_policy": "targeted_backfill",
                       "recording_started_at": epoch, "imported_at": imported_at,
                       "authorized_import_threads": result["family"]}
            provenance = {"capture_policy": "targeted_backfill", "imported_at": imported_at,
                          "authorized_root_thread": thread_id}
            for thread in result["family"]:
                row = family[thread]
                source = Path(row["path"])
                try:
                    current = home
                    for part in source.relative_to(home).parts:
                        current /= part
                        if current.is_symlink():
                            raise ValueError("A native source traverses a symbolic link")
                    with source.open("rb") as stream, connection:
                        stat = os.fstat(stream.fileno())
                        header = stream.readline(_LINE_LIMIT + 1)
                        metadata, _ = _metadata(header)
                        if not header.endswith(b"\n") or metadata["id"] != thread or _root(metadata, index) != root:
                            raise ValueError("The selected native stream changed its conversation identity")
                        task = _task(connection, metadata, root["project"], state, root, setting, home)
                        if task is None:
                            raise ValueError("A managed Codex launch is still waiting to announce its native session")
                        result.update(task_id=task["task_id"], task_path=task["task_path"])
                        offset = number = 0
                        digest = hashlib.sha256()
                        stream.seek(0)
                        added = 0
                        native_order = []
                        while offset < stat.st_size:
                            raw = stream.readline(min(_LINE_LIMIT + 1, stat.st_size - offset))
                            if len(raw) > _LINE_LIMIT:
                                raise ValueError("A native rollout line exceeds the 32 MiB capture limit")
                            if not raw.endswith(b"\n"):
                                break
                            number += 1
                            added += _record(connection, task, number, raw, source, offset, provenance=provenance)
                            native_order.append((number, hashlib.sha256(raw).hexdigest()))
                            digest.update(raw)
                            offset += len(raw)
                        connection.execute("INSERT OR REPLACE INTO streams VALUES(?,?,?,?,?,?,?,?,?,?)",
                                           (str(source), thread, f"{stat.st_dev}:{stat.st_ino}", offset, number,
                                            digest.hexdigest(), stat.st_size, stat.st_mtime_ns, epoch, 0))
                        _order_import(connection, task, native_order)
                        _publish(connection, task)
                        _task_metadata(connection, task, root, metadata, source, setting, index)
                    result["records"] += added
                except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                    result["errors"].append({"thread_id": thread, "source_path": str(source), "error": str(error)})
        result["idempotent"] = result["records"] == 0 and not result["errors"]
        return result

def sync(state_dir=None, *, project=None, max_records=_BATCH):
    if type(max_records) is not int or max_records < 1:
        raise ValueError("max_records must be a positive integer")
    state = state_root(state_dir)
    initial = _configuration(state)
    if initial.get("scope") == "all_projects":
        if initial.get("global", {}).get("enabled") and not initial["global"].get("recording_started_at"):
            configure(None, state, scope="all_projects")
    else:
        for path, setting in initial["projects"].items():
            if setting.get("enabled") and "recording_started_at" not in setting:
                configure(path, state)
    with file_lock(state / ".codex-recording.lock"):
        began = time.monotonic()
        priority_deadline = began + 1.5
        deadline = began + 3
        config = _configuration(state)
        selected = str(Path(project).expanduser().resolve()) if project is not None else None
        global_mode = config.get("scope") == "all_projects"
        if global_mode:
            selected = None
        global_enabled = global_mode and config.get("global", {}).get("enabled", False)
        policies = config["projects"]
        allowed = set() if global_mode else {path for path, row in policies.items() if row["enabled"] and (selected is None or path == selected)}
        report = {"files": 0, "threads": 0, "native_thread_count": 0, "records": 0, "errors": [], "pending": False, "synced_at": _now(), "phase": "disabled"}
        project_reports = {path: {**report, "errors": []} for path in allowed}
        project_threads = {path: set() for path in allowed}
        project_native = {path: set() for path in allowed}
        home = Path(config["codex_home"])
        if allowed or global_enabled:
            report["phase"] = "caught_up" if any((home / name).is_dir() for name in ("sessions", "archived_sessions")) else "missing_source"
            seen = set()
            native_seen = set()
            with closing(_connect(state)) as connection:
                sources = _sources(home)
                with connection:
                    metadata_index = _inventory(connection, sources)
                if global_enabled:
                    allowed = {root["project"] for row in metadata_index.values()
                               if (root := _root(row["metadata"], metadata_index)) is not None
                               and (selected is None or root["project"] == selected)}
                    policies = {path: {**config["global"], "baseline_key": _GLOBAL} for path in allowed}
                    project_reports = {path: {**report, "errors": []} for path in allowed}
                    project_threads = {path: set() for path in allowed}
                    project_native = {path: set() for path in allowed}
                if sources:
                    start = bisect_right([str(source) for source in sources], config.get("scan_cursor", "")) % len(sources)
                    sources = sources[start:] + sources[:start]
                modified = {}
                for source in sources:
                    try:
                        modified[source] = source.stat().st_mtime_ns
                    except OSError:
                        continue
                priority = sorted(modified, key=lambda source: modified[source], reverse=True)[:8]

                def consume(source, budget):
                    try:
                        with connection:
                            task, added, pending = _scan_file(connection, source, allowed, state, home, budget,
                                                               metadata_index=metadata_index, policies=policies)
                        if task:
                            report["files"] += 1
                            if task["task_mode"] != "llm_analysis":
                                seen.add(task["task_id"])
                            native_seen.add(task["thread"])
                            item = project_reports[task["project"]]
                            item["files"] += 1
                            item["records"] += added
                            item["pending"] |= pending
                            if task["task_mode"] != "llm_analysis":
                                project_threads[task["project"]].add(task["task_id"])
                            project_native[task["project"]].add(task["thread"])
                        elif pending:
                            for item in project_reports.values():
                                item["pending"] = True
                        report["records"] += added
                        report["pending"] |= pending
                    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                        detail = {"source_path": str(source), "error": str(error)}
                        report["errors"].append(detail)
                        for item in project_reports.values():
                            item["errors"].append(detail)

                prioritized = set()
                for source in priority:
                    if time.monotonic() >= priority_deadline:
                        break
                    consume(source, max_records)
                    prioritized.add(source)
                for source in sources:
                    if source in prioritized:
                        continue
                    if time.monotonic() >= deadline:
                        report["pending"] = True
                        for item in project_reports.values():
                            item["pending"] = True
                        break
                    config["scan_cursor"] = str(source)
                    consume(source, max_records)
                report["threads"] = len(seen)
                report["native_thread_count"] = len(native_seen)
            if report["errors"]:
                report["phase"] = "error"
            elif report["pending"]:
                report["phase"] = "collecting"
        config["last_sync"] = report
        for path, item in project_reports.items():
            item["threads"] = len(project_threads[path])
            item["native_thread_count"] = len(project_native[path])
            item["phase"] = "error" if item["errors"] else "collecting" if item["pending"] else "missing_source" if report["phase"] == "missing_source" else "caught_up"
        config.setdefault("sync_reports", {}).update(project_reports)
        write_json(state / "codex-recording.json", config)
    return {**status(state, project=project), **report}


def watch(state_dir=None, *, interval=2, stop_event=None):
    if type(interval) not in (int, float) or interval <= 0:
        raise ValueError("interval must be positive")
    stopped = stop_event or threading.Event()
    while not stopped.is_set():
        sync(state_dir)
        stopped.wait(interval)
