"""Map DSH job attempts and official CL-Bench checkpoints to observed facts."""

# Adapted from Skill_RSI_hzh. See LICENSE.hzh and SOURCE_PROVENANCE.json.

import json
import math
import re
from collections import defaultdict
from pathlib import Path

from ...evidence.archive import pointer
from ...evidence.schema import NativeRun, event, identifier

_JOB = re.compile(r"(?:(live|incomplete)-)?(run|baseline)-(\d+)\.json\.gz")
_RUNTIME = re.compile(r"[a-f0-9]{32}")
_CAPTURE = {
    "queries": {"query_start": "task_query", "prompt_sent": "prompt_sent",
                "query_end": "query_result", "observation": "environment_observation"},
    "boundaries": {},
    "calls": {"model_start": "native_call_start", "model_end": "model_response", "model_output": "native_call_output"},
    "memory_operations": {},
}
_SESSION = {
    "assistant/message": "model_response", "user/message": "user_message",
    "tool/call": "tool_call", "tool/result": "tool_result",
    "turn/start": "state_boundary", "turn/end": "state_boundary",
    "step/start": "state_boundary", "step/end": "state_boundary",
    "session": "state_boundary", "llm/retry": "native_retry",
    "assistant/attempt": "native_retry",
}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return value if isinstance(value, list) else []


def _runtime_id(metadata, task):
    path = Path(str(metadata.get("runtime_directory") or ""))
    return path.name if path.parent.name == task and _RUNTIME.fullmatch(path.name) else None


def _trace_key(trace, task):
    return {
        result for row in _rows(trace.get("interactions"))
        if (result := _runtime_id(_dict(_dict(row.get("response")).get("metadata")), task))
    }


def _known_runtimes(archive):
    attempts = defaultdict(set)
    for path in sorted(archive.root.glob("native/*/*/capture/boundaries.jsonl")):
        runtime, task = path.parents[1].name, path.parents[2].name
        if not _RUNTIME.fullmatch(runtime):
            continue
        # Read only the immutable reset row. Full logs are hashed by the owning attempt.
        try:
            with archive.path(path).open("rb") as stream:
                row = json.loads(stream.readline())
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("kind") == "reset" and row.get("attempt_id") and row.get("task") == task:
            attempts[task, row["attempt_id"]].add(runtime)
    return attempts


def _jobs(archive, coverage):
    represented = set()
    attempts_for_slot = defaultdict(list)
    known = defaultdict(set)
    observed = _known_runtimes(archive)
    for path in sorted(archive.root.glob("checkpoints/*/attempts/*/*/manifest.json")):
        meta = archive.json(path)
        task = path.parents[3].name
        trace_path = meta.get("trace_path")
        if not trace_path and (path.parent / "live.json.gz").is_file():
            trace_path = path.parent / "live.json.gz"
        trace = {}
        if trace_path and archive.path(trace_path).is_file():
            trace_path = archive.path(trace_path)
            if not trace_path.is_relative_to(archive.root / "checkpoints" / task):
                raise ValueError(f"Attempt trace escapes its task checkpoint directory: {path}")
            trace = archive.json(trace_path)
            if meta.get("trace_sha256") and meta["trace_sha256"] != archive.ref(trace_path)["sha256"]:
                raise ValueError(f"Attempt trace hash differs: {trace_path}")
            represented.add(trace_path)
        else:
            trace_path = None
        ids = set(observed[task, meta.get("attempt_id")])
        for value in _rows(meta.get("runtime_directories")):
            relative = Path(value)
            if relative.parent == Path("native") / task and _RUNTIME.fullmatch(relative.name):
                ids.add(relative.name)
        ids.update(_trace_key(trace, task))
        known[task].update(ids)
        phase = meta.get("phase") or trace.get("phase")
        if trace_path:
            attempts_for_slot[task, phase, meta.get("index")].append(trace_path)
        yield task, phase, meta.get("index"), meta, trace_path, trace, ids, path

    legacy = sorted(archive.root.glob("checkpoints/*/*.json.gz"),
                    key=lambda item: (item.parent.name, item.name.startswith(("live-", "incomplete-")), item.name))
    for path in legacy:
        match = _JOB.fullmatch(path.name)
        if not match or path in represented:
            continue
        snapshot, phase, index = match.groups()
        task = path.parent.name
        trace = archive.json(path)
        if any(trace == archive.json(candidate) for candidate in attempts_for_slot[task, phase, int(index)]):
            coverage.setdefault("checkpoint_aliases", []).append(archive.ref(path, layer="official_checkpoint"))
            continue
        ids = _trace_key(trace, task)
        if snapshot and ids and ids <= known[task]:
            coverage.setdefault("associated_legacy_snapshots", []).append(archive.ref(path, layer="official_checkpoint"))
            continue
        known[task].update(ids)
        yield task, phase, int(index), {}, path, trace, ids, None

    for directory in sorted(archive.root.glob("native/*/*")):
        task = directory.parent.name
        if not _RUNTIME.fullmatch(directory.name) or directory.name in known[task] or not directory.is_dir():
            continue
        root = archive.path(directory)
        has_activity = any(root.glob("request-*.json")) or any(root.glob("action-*.json"))
        has_activity |= any(path.is_file() and path.stat().st_size > 0 for path in (
            root / "capture" / "queries.jsonl", root / "usage.jsonl", root / "stdout.jsonl", root / "stderr.log",
        )) or any(root.glob("sessions/*/*/session.v4.jsonl"))
        if not has_activity:
            coverage.setdefault("unused_runtimes", []).append(root.relative_to(archive.root).as_posix())
            continue
        yield task, None, None, {}, None, {}, {root.name}, None


def _capture(archive, run, root, queries):
    rows = defaultdict(list)
    for name, types in _CAPTURE.items():
        path = root / "capture" / (name + ".jsonl")
        if not path.is_file():
            continue
        run.sources.append(archive.ref(path, layer="native_capture"))
        for row, raw, line in archive.jsonl(path, "native_capture"):
            kind = row.get("kind")
            query = row.get("query_request_id")
            if name == "queries" and kind == "query_start" and query:
                queries[query] = (row.get("instance_id"), row.get("instance_index"))
            instance_id, instance_index = queries.get(query, (row.get("instance_id"), row.get("instance_index")))
            kind_default = {"boundaries": "state_boundary", "memory_operations": "memory_operation"}.get(name)
            event_type = "skill_load" if kind == "skill_loaded" else types.get(kind, kind_default or "native_event")
            record = event(
                raw, event_type, line, native_type=kind,
                actor_type="harness", native_session_id=row.get("session_id"), timestamp=row.get("timestamp"),
                native_seq=row.get("source_seq"), request_id=query, logical_call_id=row.get("llm_call_id"),
                instance_id=instance_id, instance_index=instance_index, tool_call_id=row.get("tool_call_id"),
                turn_id=row.get("turn_id"), native_step_id=row.get("step_id"),
                operation_id=row.get("operation_id"), tool_name=row.get("tool_name"),
                application_status=row.get("status"), runtime_id=root.name,
            )
            if isinstance(row.get("view_receipts"), list):
                record["sdk_view_receipts"] = row["view_receipts"]
            if kind == "skill_loaded":
                record.update(boundary=kind, skill_sha256=row.get("sha256"), skill_load_method=row.get("method"))
            if kind == "model_end":
                record["status"] = {"unterminated": "partial"}.get(row.get("status"), row.get("status"))
                record["error_present"] = bool(row.get("error"))
            run.events.append(record)
            rows[name].append((row, record))
    for name in ("health.json", "plugin_health.json"):
        path = root / "capture" / name
        if path.is_file():
            run.sources.append(archive.ref(path, layer="capture_health"))
            health = archive.json(path)
            run.identity.setdefault("runtime_health", {})[f"{root.name}/{name}"] = {
                key: value for key, value in health.items()
                if key in {"recording_closed", "closed", "flush", "counts", "dropped_records",
                           "errors", "elapsed_ms", "overhead_ms", "write_errors", "status",
                           "close_status", "close_seconds", "source_counts", "capture_errors",
                           "durable_prefix_only", "disposed", "open_calls", "used"}
            }
    return rows


def _memory(archive, run, root, capture):
    previous = {}
    paired = {}
    results = {row.get("operation_id"): (row, record) for row, record in capture
               if row.get("kind") == "memory_result" and row.get("operation_id")}
    operations = defaultdict(list)
    for row, record in capture:
        scope = row.get("scope_path") or row.get("path")
        operation = row.get("operation_id")
        if not scope or not operation:
            continue
        artifact = identifier(root.name, str(scope))
        record["artifact_id"] = artifact
        tool = str(row.get("tool_name") or "")
        record["operation"] = row.get("operation") or {
            "read": "read", "write": "write", "edit": "update",
            "delete": "delete", "rename": "rename",
        }.get(tool.rsplit("/", 1)[-1])
        operations[operation].append(record)
        record["concurrent"] = row.get("concurrent")
        for field in ("before", "after", "readback", "proposed", "snapshot"):
            item = _dict(row.get(field))
            path = item.get("path")
            digest = item.get("sha256")
            if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
                obj = root / "capture" / "objects" / digest
                if obj.is_file():
                    content_ref = archive.ref(obj, layer="memory_snapshot")
                    if content_ref["sha256"] != digest:
                        raise ValueError(f"Memory object hash differs: {obj}")
                else:
                    content_ref = None
            else:
                content_ref = None
            if not content_ref and item.get("status") not in {"absent", "missing", "not_found", "deleted"}:
                continue
            version = identifier(artifact, record["event_id"], field)
            parent = paired.get((operation, scope)) if field != "before" else previous.get(artifact)
            if row.get("concurrent"):
                parent = None
            run.versions.append({
                "artifact_id": artifact, "artifact_role": "memory", "version_id": version,
                "parent_version_id": parent, "content_ref": content_ref,
                "content_hash": content_ref["sha256"] if content_ref else None,
                "source_event_ids": [record["event_id"]], "observation_boundary": field,
                "state": "present" if content_ref else "absent", "scope_path": scope,
                "object_path": path,
            })
            record[field + "_version_id"] = version
            if field in {"proposed", "readback"}:
                record[field + "_ref"] = content_ref
            if field == "before":
                paired[operation, scope] = version
            if field in {"before", "after", "snapshot"}:
                previous[artifact] = version
    for operation, records in operations.items():
        result, result_event = results.get(operation, ({}, {}))
        for row in records:
            if row.get("native_type") != "memory_after":
                continue
            row["source_event_ids"] = [item["event_id"] for item in records if item is not row]
            row["before_version_id"] = next(
                (item["before_version_id"] for item in records if item.get("before_version_id")), None,
            )
            success = result.get("status") == "completed" and not row.get("concurrent")
            if result_event:
                row["rendered_result_ref"] = {
                    **result_event["raw_ref"], "locator": {**result_event["raw_ref"]["locator"],
                                                         "line_pointer": "/rendered_content"},
                }
            row["application_status"] = "unknown" if not result else result.get("status")
            if success and row.get("after_version_id") and row.get("operation") in {"write", "update", "delete", "rename"}:
                row["applied_version_id"] = row["after_version_id"]
                row["application_status"] = "applied"

    memory = root / "workspace" / "memory"
    for path in sorted(memory.rglob("*")):
        if path.is_file() and not path.is_symlink():
            try:
                raw = archive.ref(path, layer="memory_final_state")
            except ValueError as exc:
                if "outside the allowed scope" not in str(exc) and "traverses a symlink" not in str(exc):
                    raise
                run.diagnostics.append({"reason": "memory_path_excluded"})
                continue
            scope = path.relative_to(memory).as_posix()
            artifact = identifier(root.name, scope)
            run.versions.append({
                "artifact_id": artifact, "artifact_role": "memory",
                "version_id": identifier(artifact, raw["sha256"], "final"),
                "parent_version_id": None, "content_ref": raw, "content_hash": raw["sha256"],
                "scope_path": scope, "observation_boundary": "workspace_final_observed",
                "state": "present", "source_event_ids": [],
            })



def decode_session_row(row, origin, *, rpc=None, session_hint=None):
    params = _dict(row.get("params"))
    is_rpc = row.get("jsonrpc") == "2.0" if rpc is None else rpc
    envelope = _dict(params.get("event")) if is_rpc else row
    kind = str(envelope.get("type") or row.get("method") or "rpc_result")
    data = _dict(envelope.get("data"))
    message = _dict(data.get("message"))
    session = params.get("sessionId") if is_rpc else session_hint or envelope.get("session_id")
    event_type = _SESSION.get(kind, "context_change" if kind.startswith(
        ("compact/", "compaction/", "context/compact")) else "native_event")
    return {
        "type": event_type, "native_type": kind, "native_session_id": session,
        "actor_type": "agent" if event_type in {"model_response", "tool_call"} else "harness",
        "timestamp": envelope.get("time"), "native_seq": envelope.get("seq"),
        "tool_call_id": data.get("callId") or message.get("toolCallId"),
        "tool_name": data.get("name") or message.get("name"),
        "turn_id": data.get("turn"), "native_step_id": data.get("step"),
        "error_present": bool(data.get("error") or message.get("isError")),
        "logical_call_id": data.get("llmCallId"),
        "origin": origin.position() if callable(getattr(origin, "position", None)) else origin,
    }

def _session(archive, run, root, *, files=None):
    tool_events = defaultdict(list)
    outputs = defaultdict(list)
    stdout = root / "stdout.jsonl"
    sessions = list(files) if files is not None else sorted(root.glob("sessions/*/*/session.v4.jsonl"))
    if files is None and stdout.is_file():
        sessions.append(stdout)
    for path in sessions:
        if path == stdout:
            rpc = True
        else:
            with archive.path(path).open("rb") as stream:
                try:
                    first = json.loads(stream.readline())
                    rpc = isinstance(first, dict) and first.get("jsonrpc") == "2.0"
                except ValueError:
                    rpc = False
        run.sources.append(archive.ref(path, layer="native_rpc" if rpc else "native_session"))
        for row, raw, seq in archive.jsonl(path, "native_rpc" if rpc else "native_session"):
            params = _dict(row.get("params"))
            envelope = _dict(params.get("event")) if rpc else row
            kind = str(envelope.get("type") or row.get("method") or "rpc_result")
            data = _dict(envelope.get("data"))
            decoded = decode_session_row(row, raw, rpc=rpc, session_hint=path.parent.name)
            type = decoded.pop("type")
            decoded.pop("origin")
            session = decoded["native_session_id"]
            record = event(raw, type, seq, runtime_id=root.name, **decoded)
            if type == "tool_call" and record["tool_name"] in {"read", "write", "edit"}:
                arguments = data.get("arguments")
                try:
                    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                except ValueError:
                    arguments = None
                path_value = _dict(arguments).get("file_path")
                if isinstance(path_value, str):
                    target = Path(path_value)
                    workspace = root / "workspace"
                    location = target if target.is_absolute() else workspace / target
                    record["memory_scoped"] = location.is_relative_to(workspace / "memory") and ".." not in location.parts
            if type == "tool_call" and record["tool_name"] == "clbench_submit":
                arguments = data.get("arguments")
                try:
                    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                    record["submitted_arguments_hash"] = identifier(arguments) if arguments is not None else None
                except ValueError:
                    record["submitted_arguments_hash"] = None
            if rpc:
                record["representation"] = "rpc"
            elif envelope.get("seq") is not None:
                record["representation"] = "session"
            run.events.append(record)
            if rpc and type in {"tool_call", "tool_result"} and record["tool_call_id"]:
                tool_events[session, record["tool_call_id"], type].append(record)
            if rpc and kind == "turn/end":
                record["terminal_reason"] = data.get("reason")
                tool_events[session, record["turn_id"], "turn_terminal"].append(record)
            if not rpc and type == "model_response" and session is not None:
                outputs[session, record["turn_id"], record["native_step_id"]].append(record)
                if envelope.get("seq") is not None:
                    outputs[session, "seq", envelope["seq"]].append(record)
    return tool_events, outputs


def _calls(archive, run, root, captured, queries, outputs):
    calls = {}
    tool_refs = defaultdict(list)
    for record in run.events:
        if (record.get("runtime_id") == root.name and record.get("representation") == "rpc"
                and record["type"] in {"tool_call", "tool_result"}):
            tool_refs[record.get("native_session_id"), record.get("turn_id"), record.get("native_step_id")].append(record["raw_ref"])

    def get(call_id):
        return calls.setdefault(call_id, {
            "logical_call_id": call_id, "attempt_id": None, "runtime_id": root.name,
            "request_ref": None, "actual_context_ref": None, "response_ref": None,
            "source_event_ids": [], "usage_refs": [], "status": "unknown",
            "request_evidence_kind": "unavailable", "state_effect": "unknown",
            "missing_reasons": ["physical_send_attempt_unobserved", "effective_endpoint_input_unverified"],
        })
    for row, record in captured:
        if not (call_id := row.get("llm_call_id")):
            continue
        call = get(call_id)
        for key in ("native_session_id", "request_id", "turn_id", "native_step_id"):
            if record.get(key) is not None:
                call[key] = record[key]
        if row.get("purpose") is not None:
            call["purpose"] = row["purpose"]
        if row.get("kind") == "model_end":
            call["status"] = {"unterminated": "partial"}.get(row.get("status"), row.get("status") or "unknown")
            if row.get("response_capture_failed"):
                call["missing_reasons"].append("response_capture_failed")
        if row.get("native_attempt_id") is not None:
            call["native_attempt_id"] = row["native_attempt_id"]
        if row.get("native_event_seq") is not None:
            call["output_native_seq"] = row["native_event_seq"]
        for field in ("request_path", "response_path"):
            value = row.get(field)
            if not value:
                continue
            path = Path(value)
            candidates = [root / path, archive.root / path] if not path.is_absolute() else [path]
            for candidate in candidates:
                if candidate.is_relative_to(root) and archive.path(candidate).is_file():
                    layer = "native_options" if field == "request_path" else "native_output_chunks"
                    ref_name = field.replace("_path", "_ref")
                    call[ref_name] = archive.ref(candidate, layer=layer)
                    record[ref_name] = call[ref_name]
                    if field == "request_path":
                        call["request_evidence_kind"] = layer
                    break
        if row.get("view_receipts"):
            call["sdk_view_receipts"] = row["view_receipts"]
        if isinstance(row.get("usage"), dict):
            call["usage"] = row["usage"]
        call["source_event_ids"].append(record["event_id"])
    usage_path = root / "usage.jsonl"
    if usage_path.is_file():
        run.sources.append(archive.ref(usage_path, layer="native_usage"))
        for row, raw, seq in archive.jsonl(usage_path, "native_usage"):
            record = event(raw, "model_usage", seq, actor_type="harness", native_session_id=row.get("sessionId"),
                           logical_call_id=row.get("callId"), timestamp=row.get("startedAt"), runtime_id=root.name)
            run.events.append(record)
            if not (call_id := row.get("callId")):
                continue
            call = get(call_id)
            for key, source in (("native_session_id", "sessionId"), ("purpose", "purpose")):
                if row.get(source) is not None:
                    call.setdefault(key, row[source])
            call["usage_refs"].append(raw)
            call["source_event_ids"].append(record["event_id"])
            if call["status"] == "unknown":
                finish = row.get("finish")
                reason = _dict(finish).get("kind", finish)
                if row.get("error") or reason == "error":
                    call["status"] = "failed"
                elif reason in ("aborted", "cancelled"):
                    call["status"] = "cancelled"
                elif reason:
                    call["status"] = "completed"
    for path in sorted(root.glob("request-*.json")):
        call_id = path.stem.removeprefix("request-")
        call = get(call_id)
        call["request_ref"] = archive.ref(path, layer="native_options")
        call["request_evidence_kind"] = "native_options"

    for call in calls.values():
        instance, index = queries.get(call.get("request_id"), (None, None))
        call.update(instance_id=instance, instance_index=index)
        call["mapping_status"] = "exact" if instance is not None else "unresolved"
        if instance is None:
            call["missing_reasons"].append("call_to_instance_unresolved")
        for name in ("request_ref", "response_ref"):
            if call[name] is None:
                call["missing_reasons"].append(f"{name}_unavailable")
        output_seq = call.get("output_native_seq")
        if output_seq is not None:
            matches = outputs[call.get("native_session_id"), "seq", output_seq]
            if len(matches) == 1:
                call["source_event_ids"].append(matches[0]["event_id"])
                if call["response_ref"] is None:
                    call["response_ref"] = matches[0]["raw_ref"]
                    call["missing_reasons"].remove("response_ref_unavailable")
        call["tool_refs"] = tool_refs[call.get("native_session_id"), call.get("turn_id"), call.get("native_step_id")] \
            if call.get("turn_id") is not None and call.get("native_step_id") is not None else []
        if call["request_ref"]:
            record = event(
                call["request_ref"], "model_request", 1, actor_type="harness",
                native_session_id=call.get("native_session_id"), logical_call_id=call["logical_call_id"],
                request_id=call.get("request_id"), instance_id=instance, instance_index=index,
                request_ref=call["request_ref"], actual_context_ref=None, attempt_id=None,
                request_evidence_kind=call["request_evidence_kind"], runtime_id=root.name,
            )
            run.events.append(record)
            call["source_event_ids"].append(record["event_id"])
        run.requests.append(call)


def _official(archive, run, trace_path, trace, runtimes, commit):
    def ref(location, layer):
        return archive.ref(trace_path, pointer(location), layer)

    instance_by_id = {}
    for i, row in enumerate(_rows(trace.get("instance_outcomes"))):
        instance_by_id[row.get("instance_id")] = row.get("instance_index")
        reward = row.get("reward")
        valid = isinstance(reward, (int, float)) and not isinstance(reward, bool) and math.isfinite(reward)
        score = {
            "instance_id": row.get("instance_id"), "instance_index": row.get("instance_index"),
            "scorer_version": commit, "grading_status": "valid" if valid else "unscorable",
            "metric_name": "reward", "raw_score": reward if valid else None,
            "raw_metric_name": row.get("raw_metric_name"),
            "details_ref": ref(f"/instance_outcomes/{i}", "evaluator_private"),
        }
        if isinstance(row.get("success"), bool):
            score["native_success"] = row["success"]
        run.scores.append(score)
        extra = row.get("raw_metric_value")
        metric = row.get("raw_metric_name")
        if metric and metric != "reward" and type(extra) in (int, float) and math.isfinite(extra):
            run.scores.append({**score, "metric_name": metric, "raw_score": extra, "grading_status": "valid"})
    for i, row in enumerate(_rows(trace.get("interactions"))):
        query, response = _dict(row.get("query")), _dict(row.get("response"))
        metadata = _dict(response.get("metadata"))
        session, tool = metadata.get("session_id"), metadata.get("tool_call_id")
        instance = query.get("instance_id")
        index = query.get("instance_index", instance_by_id.get(instance))
        runtime_id = _runtime_id(metadata, run.identity["task"])
        fields = {"instance_id": instance, "instance_index": index, "timestamp": row.get("timestamp"),
                  "native_session_id": session, "runtime_id": runtime_id,
                  "actor_type": "harness", "tool_call_id": tool}
        base = f"/interactions/{i}"
        run.events.append(event(ref(base + "/query", "task_query"), "task_query", 3 * i + 1, **fields))
        if "response" in row:
            actions, tools = runtimes.get(runtime_id, ({}, {}))
            matches = actions.get((session, tool), [])
            calls = tools.get((session, tool, "tool_call"), [])
            results = tools.get((session, tool, "tool_result"), [])
            linked = bool(session and tool and len(matches) == len(calls) == len(results) == 1)
            raw_action_ref, request_id, action = matches[0] if len(matches) == 1 else (None, None, None)
            turn_ends = tools.get((session, calls[0]["turn_id"], "turn_terminal"), []) if linked else []
            terminal = turn_ends[0].get("terminal_reason") if len(turn_ends) == 1 else None
            terminal = terminal.get("kind", terminal.get("type")) if isinstance(terminal, dict) else terminal
            committed = (linked and calls[0]["tool_name"] == "clbench_submit" and not results[0]["error_present"]
                         and calls[0].get("submitted_arguments_hash") == identifier(action)
                         and terminal == "completed")
            status = "verified_submission" if committed else "unverified"
            normalized = ref(base + "/response/action", "official_action") if "action" in response else None
            run.events.append(event(
                ref(base + "/response", "official_response"), "environment_action", 3 * i + 2, **fields,
                request_id=request_id, raw_action_ref=raw_action_ref, validated_action_ref=normalized,
                schema_ref=ref(base + "/query/response_schema", "task_schema") if "response_schema" in query else None,
                normalization_version=commit, mapping_status="exact" if linked else "unresolved",
                submission_status=status, normalization_changed=(action != response["action"])
                if normalized and raw_action_ref else None,
                source_event_ids=[item["event_id"] for item in calls + results + turn_ends],
            ))
        if "observation" in row:
            run.events.append(event(ref(base + "/observation", "environment_observation"),
                                    "environment_observation", 3 * i + 3, **fields, done=row.get("done")))
        if instance is not None and instance not in instance_by_id:
            run.scores.append({"instance_id": instance, "instance_index": index, "scorer_version": commit,
                               "grading_status": "pending", "metric_name": "reward", "raw_score": None,
                               "details_ref": None, "missing_reasons": ["native_outcome_unavailable"]})
            instance_by_id[instance] = index


def read_runs(archive, *, sources=None, coverage=None):
    if sources:
        raise ValueError("DSH reads the standard batch checkpoints and native directories")
    coverage = coverage if coverage is not None else {}
    batch_path = archive.root / "manifest.json"
    batch = archive.json(batch_path) if batch_path.is_file() else {}
    runtime = _dict(batch.get("runtime"))
    system = _dict(batch.get("system"))
    coverage["profile"] = batch.get("profile")
    coverage["tasks"] = {name: data.get("status") for name, data in _dict(batch.get("tasks")).items()}
    for task, phase, index, meta, trace_path, trace, ids, attempt_path in _jobs(archive, coverage):
        diag_start = len(archive.diagnostics)
        job = meta.get("job_id") or (f"{task}/{phase}-{index}" if phase is not None else None)
        owned = [*sorted(ids)]
        attempt = meta.get("attempt_id") or identifier(job, owned, str(trace_path.relative_to(archive.root)) if trace_path else None)
        execution = _dict(trace.get("execution"))
        run = NativeRun({
            "run_id": meta.get("run_id"), "job_id": job, "job_attempt_id": attempt, "job_index": index,
            "previous_attempt_id": meta.get("previous_attempt_id"),
            "parent_run_id": meta.get("parent_run_id"), "resume_from": meta.get("resume_from"),
            "batch_id": batch.get("run_name") or archive.root.name,
            "task": task, "phase": "rollout" if phase == "run" else phase, "native_phase": phase,
            "status": meta.get("status") or trace.get("status") or "unknown",
            "stop_reason": meta.get("stop_reason"),
            "schedule": trace.get("schedule") or meta.get("schedule"), "seed": meta.get("seed"),
            "mode": meta.get("mode") or ("reset_each_instance" if phase == "baseline" else _dict(
                _dict(batch.get("tasks")).get(task)).get("run_mode")),
            "created_at": meta.get("created_at") or execution.get("start_time"),
            "ended_at": meta.get("ended_at") or execution.get("end_time"),
            "expected_instance_count": meta.get("expected_instance_count"),
            "harness_version": runtime.get("dsh_version"), "clbench_commit": runtime.get("bench_commit"),
            "scorer_version": runtime.get("bench_commit"),
            "model_id": system.get("model"), "provider": system.get("provider"),
            "gateway": system.get("base_url"),
            "sampling": {k: system[k] for k in ("reasoning_effort", "tools_mode", "max_tokens", "max_steps", "timeout") if k in system},
            "runtime_ids": owned, "fingerprint": meta.get("fingerprint") or batch.get("fingerprint"),
            "execution_fingerprint": meta.get("execution_fingerprint") or meta.get("fingerprint") or batch.get("fingerprint"),
            "skill": meta.get("skill") or batch.get("skill_loading"), "official_usage_ref": None,
            "reset_policy": "new_session_home_workspace" if attempt_path else None,
            "memory_scope": {"memory_roots": ["workspace/memory"], "collector": "native_tool_boundaries",
                             "excluded": ["runtime_home", "symlinks_outside_memory", "log_and_capture_roots"]} if attempt_path else None,
        })
        for path, layer in ((batch_path, "batch_manifest"), (trace_path, "official_checkpoint"),
                            (attempt_path, "job_attempt"), (archive.root / "tasks" / (task + ".json.gz"), "official_viewer")):
            if path is not None and path.is_file():
                run.sources.append(archive.ref(path, layer=layer))
        runtimes = {}
        for runtime_id in owned:
            root = archive.path(Path("native") / task / runtime_id)
            if not root.is_dir():
                run.diagnostics.append({"reason": "missing_runtime_directory", "runtime_id": runtime_id})
                continue
            queries = {}
            captured = _capture(archive, run, root, queries)
            tools, outputs = _session(archive, run, root)
            resets = [item["raw_ref"] for row, item in captured["boundaries"] if row.get("kind") == "reset"]
            if resets:
                run.identity.setdefault("initial_state_refs", []).extend(resets)
            _memory(archive, run, root, captured["memory_operations"])
            _calls(archive, run, root, captured["calls"], queries, outputs)
            for name in ("stderr.log", "query.json", "bridge.patch.json"):
                path = root / name
                if path.is_file():
                    run.sources.append(archive.ref(path, layer="native_control" if name == "query.json" else "native_runtime"))
            actions = defaultdict(list)
            for path in sorted(root.glob("action-*.json")):
                raw = archive.ref(path, layer="native_action")
                row = archive.json(path)
                key = row.get("sessionId"), row.get("toolCallId")
                action = archive.ref(path, pointer("/action"), "native_action") if "action" in row else raw
                actions[key].append((action, row.get("requestId"), row.get("action")))
                run.events.append(event(raw, "action_submission", 1, actor_type="agent", request_id=row.get("requestId"),
                                        native_session_id=key[0], tool_call_id=key[1], runtime_id=runtime_id))
            runtimes[runtime_id] = actions, tools
        if trace_path:
            if "usage" in execution:
                run.identity["official_usage_ref"] = archive.ref(trace_path, pointer("/execution/usage"), "native_usage")
                measured = _dict(_dict(execution["usage"]).get("interaction"))
                cost = measured.get("cost_usd")
                exact_coverage = measured.get("pricing_complete") is True and measured.get("unpriced_call_count") == 0
                run.identity["usage"] = {
                    "cost": {"value": cost if type(cost) in (int, float) and math.isfinite(cost) else None,
                             "unit": "USD", "source": "official_per_run_usage",
                             "estimation_quality": "configured_price_estimate" if exact_coverage else "incomplete",
                             "scope_ref": run.identity["official_usage_ref"]},
                    "wall_duration_seconds": execution.get("wall_duration_seconds"),
                    "unpriced_call_count": measured.get("unpriced_call_count"),
                }
            task_config = _dict(_dict(trace.get("task")).get("params"))
            if run.identity["seed"] is None:
                run.identity["seed"] = task_config.get("seed")
            _official(archive, run, trace_path, trace, runtimes, runtime.get("bench_commit"))
        run.diagnostics.extend(archive.diagnostics[diag_start:])
        yield run
