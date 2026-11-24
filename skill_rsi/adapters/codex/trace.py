"""Read Codex schema 2 rollout traces and associate reducer evidence."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory

from ...evidence.archive import pointer
from ...evidence.schema import NativeRun, event, identifier
from ..base import probe_command
from ..native_events import mapping, records

_TYPES = {
    "inference_started": "native_call_start",
    "inference_completed": "model_response",
    "inference_failed": "model_response",
    "inference_cancelled": "model_response",
    "tool_call_started": "tool_call",
    "tool_call_ended": "tool_result",
    "codex_turn_ended": "harness_outcome",
    "rollout_ended": "harness_outcome",
    "history_item_accepted": "context_change",
}
_ENDPOINT = {"inference_completed": "completed", "inference_failed": "failed",
             "inference_cancelled": "cancelled"}


def _payload_descriptors(value):
    if isinstance(value, dict):
        if isinstance(value.get("raw_payload_id"), str) and isinstance(value.get("path"), str):
            yield value
        else:
            for item in value.values():
                yield from _payload_descriptors(item)
    elif isinstance(value, list):
        for item in value:
            yield from _payload_descriptors(item)


def _relative(root, supplied):
    path = Path(supplied)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError(f"Unsafe Codex trace path: {supplied!r}")
    return root / path


def _prefix(archive, bundle, trace, manifest, run):
    expected = 1
    payloads = {}
    prefix = []
    trace_ref = archive.ref(trace, layer="native_trace")
    payload_root = _relative(bundle, manifest.get("payloads_dir") or "payloads")
    with archive.path(trace).open("rb") as stream:
        for line, raw in enumerate(stream, 1):
            ref = {**trace_ref, "locator": {"type": "line", "line": line}}
            if not raw.endswith(b"\n"):
                run.diagnostics.append({"reason": "truncated_record", "raw_ref": ref})
                break
            try:
                row = json.loads(raw)
                if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
                    raise ValueError("expected trace event")
            except (ValueError, UnicodeError):
                run.diagnostics.append({"reason": "malformed_record", "raw_ref": ref})
                break
            if row.get("seq") != expected or row.get("schema_version") != 2 or row.get("rollout_id") != manifest["rollout_id"]:
                run.diagnostics.append({"reason": "trace_discontinuity", "expected_seq": expected,
                                        "observed_seq": row.get("seq"), "raw_ref": ref})
                break
            try:
                local = {}
                for descriptor in _payload_descriptors(row["payload"]):
                    payload_id = descriptor["raw_payload_id"]
                    path = _relative(bundle, descriptor["path"])
                    if not path.is_relative_to(payload_root):
                        raise ValueError(f"Payload outside declared payloads_dir: {payload_id}")
                    path = archive.path(path)
                    if not path.is_file():
                        raise FileNotFoundError(payload_id)
                    ref_body = archive.ref(path, layer="codex_raw_payload")
                    previous = local.get(payload_id) or payloads.get(payload_id)
                    if previous and previous != ref_body:
                        raise ValueError(f"Multiple files for payload {payload_id}")
                    local[payload_id] = ref_body
                payloads.update(local)
            except (FileNotFoundError, ValueError) as error:
                run.diagnostics.append({"reason": "missing_or_invalid_native_payload",
                                        "raw_ref": ref, "detail": str(error)})
                break
            prefix.append((row, ref, raw))
            expected += 1
    archive.assert_unchanged(trace)
    return prefix, payloads


def _reduce(archive, bundle, manifest_path, trace, prefix, payloads, command, run):
    if not command or not probe_command(command, "debug", "trace-reduce", "--help"):
        run.diagnostics.append({"reason": "missing_compatible_trace_reducer"})
        return {}
    # Reduce an isolated, verified prefix. The original evidence directory stays read-only.
    with TemporaryDirectory(prefix="skill-rsi-codex-trace-") as temp:
        staging = Path(temp)
        shutil.copyfile(manifest_path, staging / "manifest.json")
        path = staging / trace.relative_to(bundle)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(raw for _, _, raw in prefix))
        manifest = archive.json(manifest_path)
        _relative(staging, manifest.get("payloads_dir") or "payloads").mkdir(parents=True, exist_ok=True)
        for reference in payloads.values():
            original = archive.path(reference["path"])
            destination = staging / original.relative_to(bundle)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, destination)
            archive.assert_unchanged(original)
        try:
            result = subprocess.run(
                [str(command), "debug", "trace-reduce", str(staging)], capture_output=True,
                stdin=subprocess.DEVNULL, timeout=90, check=False,
            )
            if result.returncode:
                run.diagnostics.append({"reason": "trace_reducer_rejected_prefix", "returncode": result.returncode})
                return {}
            with (staging / "state.json").open(encoding="utf-8") as stream:
                return mapping(json.load(stream))
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            run.diagnostics.append({"reason": "trace_reducer_unavailable", "detail": type(error).__name__})
            return {}


def _payload(payloads, item, field):
    value = item.get(field)
    if isinstance(value, dict):
        return payloads.get(value.get("raw_payload_id"))
    return payloads.get(value) if isinstance(value, str) else None


def _calls(archive, prefix, payloads, state, run):
    starts = {}
    terminal = {}
    transport = defaultdict(list)
    tool_events = {}
    for row, source, _ in prefix:
        body = row["payload"]
        kind = body.get("type")
        call = body.get("inference_call_id")
        tool = body.get("tool_call_id")
        thread = row.get("thread_id") or body.get("thread_id")
        turn = row.get("codex_turn_id") or body.get("codex_turn_id")
        event_type = _TYPES.get(kind, "context_change" if "compact" in str(kind) else "native_event")
        if kind == "thread_started":
            event_type = "session_resume" if body.get("resumed_from") else "session_start"
        if "compact" in str(kind) and "install" in str(kind):
            event_type = "compaction_installed"
        full = event(
            source, event_type, row["seq"], native_type=kind,
            native_seq=row["seq"], native_session_id=thread, thread_id=thread, turn_id=turn,
            timestamp=row.get("wall_time_unix_ms"), logical_call_id=call,
            tool_call_id=tool, actor_type="harness", status=body.get("status") or _ENDPOINT.get(kind),
            role="main" if thread == run.identity.get("root_thread_id") else "child" if thread else "unknown",
            model_visible_call_id=body.get("model_visible_call_id"), teacher_visible=event_type != "native_event",
        )
        if kind == "tool_call_started":
            tool_events[tool] = full
        elif kind == "tool_call_ended":
            ref = _payload(payloads, body, "result_payload")
            full.update(rendered_result_ref=ref, parent_event_id=(tool_events.get(tool) or {}).get("event_id"))
        elif kind == "inference_started" and call:
            if call in starts:
                run.diagnostics.append({"reason": "duplicate_inference_identity", "raw_ref": source})
            starts[call] = (body, full)
        elif kind in _ENDPOINT and call:
            terminal[call] = (body, full)
        elif call and ("transport" in str(kind) or str(kind).startswith(("http_", "ws_", "websocket_"))):
            transport[call].append((body, full))
        run.events.append(full)
    inferred = mapping(state.get("inference_calls"))
    known_items = mapping(state.get("conversation_items"))
    tools = mapping(state.get("tool_calls"))
    output_roles = defaultdict(list)
    for tool_id, tool in tools.items():
        for item_id in records(mapping(tool).get("model_visible_output_item_ids")):
            output_roles[item_id].append(tool_id)
    response_prefixes = {}
    for call_id, (body, started) in starts.items():
        call = mapping(inferred.get(call_id))
        response, finished = terminal.get(call_id, ({}, {}))
        request = _payload(payloads, body, "request_payload")
        response_ref = _payload(payloads, response, "response_payload")
        inputs = call.get("request_item_ids")
        validated_ids = isinstance(inputs, list) and all(isinstance(item, str) and item in known_items for item in inputs)
        native_request = {}
        if request:
            try:
                native_request = mapping(archive.json(request["path"]))
            except (ValueError, OSError):
                run.diagnostics.append({"reason": "unreadable_logical_request", "raw_ref": request})
        raw_inputs = native_request.get("input")
        previous_response = native_request.get("previous_response_id") or call.get("previous_response_id")
        previous_response = previous_response if isinstance(previous_response, str) else None
        direct = validated_ids and isinstance(raw_inputs, list) and len(raw_inputs) == len(inputs) and not previous_response
        logical = {**request, "locator": pointer("/input"), "payload_layer": "codex_logical_input"} if direct else None
        previous = response_prefixes.get((started["thread_id"], previous_response))
        prefix_verified = bool(validated_ids and previous and inputs[:len(previous["items"])] == previous["items"])
        missing = [] if logical else ["incremental_request_source_segments_required" if prefix_verified
                                      else "unreconstructed_previous_response_prefix" if previous_response
                                      else "unverified_complete_logical_input"]
        request_record = {
            "request_id": identifier(run.identity["job_attempt_id"], call_id),
            "logical_call_id": call_id, "native_session_id": started["native_session_id"],
            "thread_id": started["thread_id"], "turn_id": started["turn_id"],
            "request_ref": request, "logical_input_ref": logical, "actual_context_ref": None,
            "response_ref": response_ref, "status": finished.get("status") or "started",
            "model": body.get("model"), "provider": body.get("provider_name"),
            "purpose": "main" if started.get("role") == "main" else "child" if started.get("role") == "child" else None,
            "response_id": response.get("response_id"), "event_id": started["event_id"],
            "source_event_ids": [row["event_id"] for row in (started, finished) if row],
            "missing_reasons": missing, "previous_response_id": previous_response,
            "previous_response_prefix_verified": prefix_verified if previous_response else None,
            "prior_input_refs": previous["refs"] if prefix_verified else [],
            "attempt_id": None, "mapping_status": "native_inference_id",
            "request_item_ids": inputs if validated_ids else None,
            "response_item_ids": call.get("response_item_ids"),
            "tool_outputs_in_logical_input": [
                {"input_item_id": item, "tool_call_id": candidates[0] if len(candidates) == 1 else None,
                 "mapping_status": "unique" if len(candidates) == 1 else "ambiguous"}
                for item in inputs if (candidates := output_roles.get(item))
            ] if validated_ids else None,
            "physical_request_status": "unknown",
            "logical_input_status": "observed" if logical else "reduced_items" if prefix_verified else "unknown",
            "transport_receipts": [
                {"attempt_id": r.get("attempt_id") or r.get("transport_attempt_id"),
                 "native_type": event_row["native_type"], "status": event_row["status"],
                 "raw_ref": event_row["raw_ref"]}
                for r, event_row in transport[call_id]
            ],
            "usage": call.get("usage") or response.get("usage"), "duration_ms": call.get("duration_ms"),
        }
        # Link model boundary only to a reduced, complete input. SDK-level logical evidence
        # is preserved separately from wire sends or server-side processing.
        run.requests.append(request_record)
        response_id = request_record["response_id"]
        output_ids = request_record["response_item_ids"]
        if started["thread_id"] and isinstance(response_id, str) and (logical or prefix_verified) and isinstance(output_ids, list):
            response_prefixes[started["thread_id"], response_id] = {
                "items": [*inputs, *output_ids],
                "refs": [*request_record["prior_input_refs"], request, *([response_ref] if response_ref else [])],
            }


def read_bundle(archive, bundle, *, command="codex", task=None):
    manifest_path = archive.path(bundle / "manifest.json")
    manifest = mapping(archive.json(manifest_path))
    required = ("trace_id", "rollout_id", "root_thread_id")
    if manifest.get("schema_version") != 2 or any(not isinstance(manifest.get(key), str) or not manifest[key] for key in required):
        raise ValueError(f"Unsupported Codex trace manifest: {manifest_path}")
    trace = archive.path(_relative(bundle, manifest.get("raw_event_log") or "trace.jsonl"))
    run = NativeRun({
        "batch_id": archive.root.name, "job_attempt_id": identifier("codex", manifest["trace_id"], manifest["rollout_id"]),
        "root_thread_id": manifest["root_thread_id"], "trace_id": manifest["trace_id"],
        "rollout_id": manifest["rollout_id"], "task": task, "status": "unknown",
        "created_at": manifest.get("started_at_unix_ms"), "trace_schema_version": 2,
        "harness_version": manifest.get("cli_version") or manifest.get("codex_version"),
        "capture_capabilities": {"physical_requests": "unknown"},
    }, sources=[archive.ref(manifest_path, layer="codex_trace_manifest")])
    if not trace.is_file():
        run.diagnostics.append({"reason": "missing_native_trace"})
        yield run
        return
    run.sources.append(archive.ref(trace, layer="native_trace"))
    prefix, payloads = _prefix(archive, bundle, trace, manifest, run)
    run.sources.extend(payloads.values())
    archive.assert_unchanged(manifest_path)
    if not prefix:
        yield run
        return
    state = _reduce(archive, bundle, manifest_path, trace, prefix, payloads, command, run)
    receipt = prefix[-1][1]
    footer = prefix[-1][0]["payload"].get("type") == "recording_completed"
    ends = [row["payload"].get("status") for row, _, _ in prefix if row["payload"].get("type") == "rollout_ended"]
    blocked_prefix = any(row["reason"] in {"truncated_record", "malformed_record", "trace_discontinuity",
                                          "missing_or_invalid_native_payload"} for row in run.diagnostics)
    closed = not blocked_prefix and (state.get("recording_complete") is True if state else footer)
    run.identity.update(
        status=state.get("status") or (ends[-1] if ends else None) or "unknown",
        recording_complete=closed, runtime_health={"trace": {
            "recording_closed": closed, "start_cursor": {"seq": prefix[0][0]["seq"]},
            "end_cursor": {"seq": prefix[-1][0]["seq"], "raw_ref": receipt}, "sampling_rule": "verified_contiguous_prefix",
            "gap_ranges": None if closed else [{"after_seq": prefix[-1][0]["seq"], "end_unknown": True}],
        }},
        reducer_version=probe_command(command, "--version") if state else None,
        trace_cursor={"source_identity": manifest["trace_id"], "seq": prefix[-1][0]["seq"], "raw_ref": receipt},
        capture_capabilities={"logical_input": "observed_subset" if state else "unknown", "physical_requests": "unknown"},
        interaction_edges=state.get("interaction_edges"), compactions=state.get("compactions"),
        compaction_requests=state.get("compaction_requests"), terminal_operations=state.get("terminal_operations"),
    )
    _calls(archive, prefix, payloads, state, run)
    yield run
