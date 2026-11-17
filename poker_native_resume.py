"""Validate and attach a Poker attempt's acknowledged native session prefix."""

from dataclasses import dataclass
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from types import MethodType


@dataclass(frozen=True)
class _NativePrefix:
    session_id: str
    header: dict
    events: bytes
    usage: bytes
    memory: dict
    lineage: dict
    uses_absolute_paths: bool


def _require(condition, message):
    if not condition:
        raise ValueError(f"Cannot resume Poker native session: {message}")


def _lines(path):
    raw = path.read_bytes().splitlines(keepends=True)
    _require(all(line.endswith(b"\n") for line in raw), f"incomplete JSONL in {path.name}")
    return raw, [json.loads(line) for line in raw]


def _one(items, message):
    items = list(items)
    _require(len(items) == 1, message)
    return items[0]


def _contained(root, value):
    path = root / value
    _require(path.is_relative_to(root) and path.resolve() == path, "path is outside its scope or symlinked")
    return path


def _memory_name(value):
    _require(isinstance(value, str) and bool(value) and Path(value).parts
             and not Path(value).is_absolute() and ".." not in Path(value).parts, "invalid memory filename")
    return value


def _memory_object(directory, description):
    digest = description.get("sha256")
    _require(description.get("status") == "captured" and description.get("representation") == "file_bytes"
             and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest), "missing captured memory bytes")
    obj = _contained(directory, f"capture/objects/{digest}").read_bytes()
    _require(len(obj) == description.get("bytes") and sha256(obj).hexdigest() == digest, "memory object hash differs")
    return obj


def _memory_at(directory, events, cutoff, seed):
    path = _contained(directory, "capture/memory_operations.jsonl")
    rows = _lines(path)[1] if path.is_file() else []
    captures = {}
    for row in rows:
        captures.setdefault(row.get("tool_call_id"), []).append(row)
    initial = {
        _memory_name(name): _memory_object(directory, desc)
        for name, desc in (seed.get("memory_files") or {}).items()
    }
    initial_cut = seed.get("session_terminal_seq", -1)
    _require(type(initial_cut) is int and initial_cut < cutoff, "resume seed is beyond the selected boundary")
    memory = initial.copy()
    at_cut = initial.copy()
    uses_absolute_paths = bool(seed.get("uses_absolute_paths"))
    allowed = {"clbench_submit", "read", "write", "edit"}
    for row in events:
        if row.get("type") != "tool/call" or row["seq"] <= initial_cut:
            continue
        call = row["data"]
        _require(call["name"] in allowed, "unobserved native tool effects")
        if call["name"] == "clbench_submit":
            continue
        operations = captures.get(call.get("callId"), [])
        before = _one((item for item in operations if item["kind"] == "memory_before"),
                      "native memory call lacks its preimage")
        after = _one((item for item in operations if item["kind"] == "memory_after"),
                     "native memory call lacks its postimage")
        name = _memory_name(before["scope_path"])
        _require(after.get("scope_path") == name and after.get("operation_id") == before.get("operation_id")
                 and after.get("session_id") == before.get("session_id")
                 and before.get("tool_name") == after.get("tool_name") == call["name"]
                 and after.get("concurrent") is False, "memory operations are ambiguous or overlapped")
        uses_absolute_paths |= Path(before["arguments"]["file_path"]).is_absolute()
        state = before["before"]
        expected = None if state.get("status") == "absent" else _memory_object(directory, state)
        _require(memory.get(name) == expected, "recorded memory transitions do not form a chain")
        state = after["after"]
        if state.get("status") == "absent":
            memory.pop(name, None)
        else:
            memory[name] = _memory_object(directory, state)
        if row["seq"] <= cutoff:
            if name in memory:
                at_cut[name] = memory[name]
            else:
                at_cut.pop(name, None)
    actual = {}
    workspace = _contained(directory, "workspace")
    for entry in workspace.rglob("*"):
        _require(not entry.is_symlink() and (entry.is_dir() or entry.is_file()),
                 "untracked workspace object")
        if entry.is_file():
            _require(entry.is_relative_to(workspace / "memory"), "untracked file outside memory")
            actual[entry.relative_to(workspace / "memory").as_posix()] = entry.read_bytes()
    _require(actual == memory, "final workspace does not match recorded memory transitions")
    return at_cut, uses_absolute_paths


def prepare_native_resume(batch, attempt, prefix):
    """Prove the native turn, task acknowledgement, usage and memory for the chosen prefix."""
    batch = Path(batch).resolve(strict=True)
    _require(attempt.get("task") == "exploitable_poker" and attempt.get("phase") == "run"
             and attempt.get("status") in ("failed", "interrupted"), "source is not a failed Poker rollout")
    trace_path = _contained(batch, attempt["trace_path"])
    _require(trace_path.parent.parent == batch / "checkpoints/exploitable_poker/attempts" / f"run-{attempt['index']}"
             and trace_path.parent.name == attempt["attempt_id"], "trace is outside its attempt directory")
    compressed = trace_path.read_bytes()
    trace_digest = sha256(compressed).hexdigest()
    _require(trace_digest == attempt.get("trace_sha256"), "source trace hash differs")
    source = json.loads(gzip.decompress(compressed))
    interactions = prefix["interactions"]
    _require(interactions and source["interactions"][:len(interactions)] == interactions
             and source["instance_outcomes"] == prefix["instance_outcomes"]
             and source["task"] == prefix["task"] and source["execution"]["run_index"] == attempt["index"],
             "provided prefix differs from source trace")
    boundary = interactions[-1]
    response = boundary["response"]
    meta = response["metadata"]
    directory = _contained(batch, Path(meta["runtime_directory"]))
    relative = directory.relative_to(batch).as_posix()
    _require(directory.parent == batch / "native/exploitable_poker"
             and relative in attempt["runtime_directories"], "runtime is outside the source attempt")
    session = meta.get("session_id")
    _require(isinstance(session, str) and re.fullmatch(r"clbench-[0-9a-f]{32}", session), "invalid session ID")
    _, boundaries = _lines(_contained(directory, "capture/boundaries.jsonl"))
    _require(any(row.get("kind") == "process_exit" and row.get("session_id") == session
                 and type(row.get("exit_code")) is int for row in boundaries), "source DSH process lacks an exit record")
    seed = next((row for row in boundaries if row.get("kind") == "native_resume" and row.get("session_id") == session), {})
    path = _one(directory.glob(f"sessions/*/{session}/session.v4.jsonl"), "session log must be unique")
    raw, rows = _lines(_contained(directory, path))
    _require(rows and rows[0].get("type") == "session" and rows[0].get("id") == session
             and rows[0].get("version") == 4 and Path(rows[0]["cwd"]) == directory / "workspace"
             and path.parent.parent.name == _project_key(directory / "workspace")
             and all(row.get("seq") == index for index, row in enumerate(rows[1:])),
             "session header or sequence does not match the native runtime")
    call_id = meta["tool_call_id"]
    marker = _one((row for row in rows if row.get("type") == "tool/call"
                   and row["data"].get("name") == "clbench_submit" and row["data"].get("callId") == call_id),
                  "trace action is absent or ambiguous in the session")
    turn = marker["data"]["turn"]
    terminal = _one((row for row in rows if row.get("type") == "turn/end" and row["data"].get("turn") == turn),
                    "submission turn is absent or ambiguous")
    cutoff = terminal["seq"]
    result = _one((row for row in rows[marker["seq"] + 2:cutoff + 2] if row.get("type") == "tool/result"
                   and row["data"].get("message", {}).get("toolCallId") == call_id),
                  "submission has no unique native tool result")
    _require(terminal["data"]["reason"]["kind"] == "completed"
             and not result["data"]["message"].get("isError"), "submission did not complete")
    _, queries = _lines(_contained(directory, "capture/queries.jsonl"))
    query_id = meta["query_request_id"]
    confirmed = _one((row for row in queries if row.get("query_request_id") == query_id and row.get("kind") == "query_end"),
                     "source submission has no unique driver acknowledgement")
    observed = _one((row for row in queries if row.get("query_request_id") == query_id and row.get("kind") == "observation"),
                    "source action has no unique benchmark observation")
    raw_observation = observed["observation"]
    traced_observation = boundary["observation"]
    _require(confirmed.get("status") == "completed" and confirmed.get("tool_call_id") == call_id
             and confirmed.get("normalized_action") == response["action"]
             and all(raw_observation.get(key) == traced_observation.get(key) for key in ("content", "instance_complete"))
             and (raw_observation.get("metadata") or {}) == (traced_observation.get("metadata") or {}),
             "driver or benchmark acknowledgement differs")
    ledger, usage = _lines(_contained(directory, "usage.jsonl"))
    required = [
        event["metadata"]["call_id"]
        for step in interactions for phase in ("respond", "observe")
        for event in step.get("usage", {}).get(phase, {}).get("events", [])
        if event.get("metadata", {}).get("call_id")
    ]
    _require(required and len(set(required)) == len(required)
             and len(usage) >= len(required)
             and {row.get("callId") for row in usage[:len(required)]} == set(required),
             "usage rows do not match the acknowledged actions")
    memory, uses_absolute_paths = _memory_at(directory, rows[1:], cutoff, seed)
    session_bytes = b"".join(raw[1:cutoff + 2])
    usage_bytes = b"".join(ledger[:len(required)])
    return _NativePrefix(session, rows[0], session_bytes, usage_bytes, memory, {
        "source_attempt_id": attempt["attempt_id"], "source_trace": attempt["trace_path"],
        "source_trace_sha256": trace_digest, "source_runtime": relative,
        "source_session_sha256": sha256(b"".join(raw)).hexdigest(), "session_terminal_seq": cutoff,
        "retained_session_events_sha256": sha256(session_bytes).hexdigest(),
        "retained_usage_sha256": sha256(usage_bytes).hexdigest(), "retained_usage_rows": len(required),
        "retained_episodes": len(prefix["instance_outcomes"]), "retained_interactions": len(interactions),
    }, uses_absolute_paths)


def _project_key(workspace):
    encoded = str(workspace).encode("utf-16-le")
    parts = []
    for offset in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[offset:offset + 2], "little")
        char = chr(unit)
        if char in "/\\:":
            if not offset or chr(int.from_bytes(encoded[offset - 2:offset], "little")) not in "/\\:":
                parts.append("-")
        else:
            parts.append(char if re.fullmatch(r"[A-Za-z0-9._-]", char) else f"~{unit:04X}")
    return "--" + ("".join(parts).lstrip("-") or "root")[:251] + "--"


def restore_native_resume(system, bundle, prefix):
    """Copy the acknowledged state into the new runtime and attach it at first SDK start."""
    target = system.directory
    workspace = system.workspace
    _require(system._process is None and not system._used and not (target / "sessions").exists()
             and not system.usage_file.exists() and not any((workspace / "memory").iterdir()), "target runtime is not fresh")
    header = {**bundle.header, "cwd": str(workspace)}
    path = target / "sessions" / _project_key(workspace) / bundle.session_id / "session.v4.jsonl"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_bytes((json.dumps(header, ensure_ascii=False) + "\n").encode() + bundle.events)
    system.usage_file.write_bytes(bundle.usage)
    objects = target / "capture/objects"
    objects.mkdir(mode=0o700)
    memory_files = {}
    for name, data in bundle.memory.items():
        dest = _contained(workspace / "memory", name)
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest.write_bytes(data)
        digest = sha256(data).hexdigest()
        (objects / digest).write_bytes(data)
        memory_files[name] = {"status": "captured", "sha256": digest, "bytes": len(data), "representation": "file_bytes"}
    system.session_id = bundle.session_id
    system._usage_offset = len(bundle.usage)
    system._interaction_step = len(prefix["interactions"])
    last = prefix["interactions"][-1]
    system._current_query = {k: last["query"][k] for k in ("instance_id", "instance_index")}
    system._feedback = [last["observation"]["content"]]
    if bundle.uses_absolute_paths:
        system._feedback.append(f"The private workspace was relocated. Continue using the memory files in {workspace / 'memory'}.")
    lineage = {**bundle.lineage, "session_id": bundle.session_id, "memory_files": memory_files,
               "uses_absolute_paths": bundle.uses_absolute_paths, "source_workspace": bundle.header["cwd"],
               "restored_workspace": str(workspace), "relocation_feedback_added": bundle.uses_absolute_paths}
    system._record("boundaries", "native_resume", **lineage)
    start = system._start

    def attach(self):
        was_stopped = self._process is None
        start()
        if was_stopped and self.directory == target:
            try:
                self._request("session/resume", {"sessionId": bundle.session_id}, time.monotonic() + min(self.timeout, 60))
            except BaseException:
                self._stop_process("native_resume_failure")
                raise

    system._start = MethodType(attach, system)
    return lineage
