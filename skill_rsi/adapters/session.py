"""Read explicit native session and execution streams."""

from collections import defaultdict

from ..evidence.archive import pointer
from ..evidence.schema import NativeRun, event, identifier
from .base import read_optional
from .native_events import mapping, native_parts, native_session


def jsonl_sources(archive, source, options, names):
    explicit = (options or {}).get("sources")
    if explicit is not None:
        files = [archive.path(path) for path in explicit]
    elif source.is_file():
        files = [source]
    else:
        files = []
        for name in names:
            files.extend(source.rglob(name))
    if explicit is None and source.is_dir():
        files = [path for path in files if not {"derived", ".git", ".skill-rsi"}.intersection(path.relative_to(source).parts)]
    paths = sorted(set(files))
    if not paths or any(not path.is_file() for path in paths):
        raise ValueError(f"No readable native records selected at {source}")
    return paths



def decode_native(row, origin, harness):
    """Decode an already selected record without opening any source files."""
    body = mapping(row.get("payload"))
    session = native_session(row, harness)
    turn = row.get("turn_id") or body.get("turn_id")
    for kind, path, fields in native_parts(row, harness):
        if row.get("type") == "event_msg" and body.get("type") in {"task_complete", "task_started"}:
            kind = "harness_outcome" if body["type"] == "task_complete" else "state_boundary"
            fields = {"actor_type": "harness", "boundary": body["type"],
                      "status": "completed" if kind == "harness_outcome" else None}
            path = "/payload/type"
        yield {
            "type": kind, "native_type": body.get("type") or row.get("type"), "native_session_id": session,
            "thread_id": session if harness == "codex" else None, "turn_id": turn, "timestamp": row.get("timestamp"),
            "native_id": row.get("uuid"), "native_parent_id": row.get("parentUuid"), "line_pointer": path or None,
            "origin": origin.position() if callable(getattr(origin, "position", None)) else origin,
            **fields,
        }

def read_session(archive, source, harness):
    layer = "native_cli" if source.name == "stdout.jsonl" else "native_session"
    full_ref = archive.ref(source, layer=layer)
    launch_path = source.parent / "launch.json"
    launch = read_optional(archive.path(launch_path)) if launch_path.exists() else None
    sessions = {}
    diagnostics_start = len(archive.diagnostics)
    current = (launch or {}).get("session_id")
    task = (launch or {}).get("task")

    def run_for(session_id):
        if session_id not in sessions:
            identity = {
                "batch_id": archive.root.name, "job_attempt_id": identifier(harness, full_ref["path"], session_id),
                "native_session_id": session_id, "task": task,
                "status": (launch or {}).get("status", "unknown"), "resume_from": (launch or {}).get("resume_from"),
                "harness_version": (launch or {}).get("harness_version"),
                "capture_capabilities": {"physical_requests": "unknown", "logical_input": "unknown"},
            }
            sessions[session_id] = NativeRun(identity, sources=[full_ref])
            if launch is not None:
                sessions[session_id].sources.append(archive.ref(launch_path, layer="native_process_receipt"))
        return sessions[session_id]

    turns = {}
    tool_calls = defaultdict(list)
    results = []
    for row, raw_ref, sequence in archive.jsonl(source, layer):
        # Native Claude transcripts use camelCase sessionId and CLI events use session_id.
        current = native_session(row, harness) or current
        run = run_for(current)
        body = mapping(row.get("payload"))
        if row.get("type") == "session_meta":
            run.identity["created_at"] = body.get("timestamp") or row.get("timestamp")
            run.identity["workspace"] = body.get("cwd")
            run.identity["harness_version"] = body.get("cli_version") or run.identity["harness_version"]
            # Retain fork provenance only when it has a native identity.
            run.identity["parent_thread_id"] = body.get("forked_from_id")
        if row.get("type") == "turn_context" or (row.get("type") == "event_msg" and body.get("type") == "task_started"):
            turns[current] = body.get("turn_id") or body.get("id")
        turn = row.get("turn_id") or body.get("turn_id") or turns.get(current)
        for fields in decode_native(row, raw_ref, harness):
            kind = fields.pop("type")
            path = fields.pop("line_pointer")
            fields.pop("origin")
            reference = {**raw_ref, "locator": {**raw_ref["locator"], **({"line_pointer": path} if path else {})}}
            if kind == "harness_outcome" and fields.get("status") in {"completed", "failed"}:
                # A task turn is complete independently of CLI process or benchmark outcome.
                run.identity["last_turn_status"] = fields["status"]
                if harness == "claude_code" and row.get("type") == "result" and launch is None:
                    run.identity["status"] = fields["status"]
            if kind == "tool_result":
                fields["rendered_result_ref"] = reference
            if kind == "state_boundary" and fields.get("boundary") in {"thread.started", "session_present", "session_meta"}:
                kind = "session_resume" if (launch or {}).get("resume_from") else "session_start"
            fields.update(native_session_id=current, thread_id=current if harness == "codex" else None, turn_id=turn)
            item = event(reference, kind, sequence, **fields)
            run.events.append(item)
            key = current, item.get("tool_call_id")
            if key[1] and kind == "tool_call":
                tool_calls[key].append(item)
            elif key[1] and kind == "tool_result":
                results.append((key, item))
    for key, result in results:
        candidates = tool_calls[key]
        if len(candidates) == 1:
            result.update(parent_event_id=candidates[0]["event_id"], mapping_status="native_call_id")
        else:
            result["mapping_status"] = "ambiguous" if candidates else "unresolved"
    if not sessions:
        run_for(current)
    for run in sessions.values():
        run.diagnostics.extend(archive.diagnostics[diagnostics_start:])
        if not run.requests:
            run.diagnostics.append({"reason": "missing_logical_request_observation"})
        yield run
