import json
import re
from pathlib import Path

from .artifacts import SCHEMA, checked_bytes, digest, encode, identifier, indexed_run, inside, read_ref


INDEX_FILES = (
    "manifest.json", "events.jsonl", "requests/index.jsonl", "trajectory.ctx.json",
    "context_episodes.jsonl", "artifacts.json", "quality.json",
)
PRIVATE_EXCLUSIONS = [
    "official environment data", "hidden tests and reference answers", "scorer private details",
    "holdout runs", "credentials", "private reasoning",
]
REF_FIELDS = {
    "raw_ref", "request_ref", "actual_context_ref", "response_ref", "readback_ref",
    "validated_action_ref", "schema_ref",
}
VISIBLE_LAYERS = {
    "native_capture", "native_events", "native_rpc", "native_options", "native_output_chunks",
    "native_action", "native_usage", "task_query", "task_schema", "official_action", "environment_observation",
}
OFFICIAL_DISPLAY = re.compile(
    r"(?:/instance_traces/\d+)?/(?:interactions/\d+/(?:query(?:/response_schema)?|observation|response/action)"
    r"|system_artifacts/(?:jsonl_events/\d+(?:/.*)?|(?:claude_code|codex)_conversation_jsonl_files/[^/]+))\Z"
)
METADATA_FIELDS = set("""
    event_id seq source_id type native_type label_status run_id task task_name schedule seed phase mode status
    instance_id instance_index actor_id actor_type parent_event_id request_id
    native_session_id thread_id logical_call_id attempt_id tool_call_id tool_name operation
    operation_id artifact_id before_version_id applied_version_id application_status
    purpose outcome request_evidence_kind mapping_status usage_status changed_fields
    metric_name grading_status scorer_version
    change_kind capture_status context_visibility generator_version schema_version
    context_episode_id label_id label_scope behavior_tags evidence_status label_method
    label_version label_confidence source_event_ids continued_event_ids downstream_status
    trajectory_id parent_trajectory_id segment_id parent_segment_id step_id step_unit
    state_effect harness_name harness_version model_id provider visibility
    input_tokens output_tokens total_tokens cache_read_tokens cache_write_tokens
    status_reason missing_reasons tool_refs usage_refs boundary skill_sha256 skill_load_method
    label_confidence trigger_status application_status recording_closed diagnostics
""".split()) | REF_FIELDS
DISPLAY_FIELDS = set("""
    role type kind name message messages text content systemPrompt system_prompt system
    input output result tool_calls tool_use tool_result function callId toolCallId
    arguments args action tool_name error records item response blocks
    query feedback actual_prompt prompt file_path path operation old_string new_string
    command edit summary instruction prompt_text allowed_feedback data event payload chunk delta
    usage total_tokens input_tokens output_tokens cached_input_tokens stop_reason
""".split())
PRIVATE_BLOCKS = {
    "thinking", "redacted_thinking", "reasoning", "reasoning_content", "internal",
    "analysis", "encrypted_reasoning", "thinking_delta", "signature_delta",
}
SECRET = re.compile(
    r"(?i)\b(?:bearer\s+[a-z0-9._/+\-=]{8,}|(?:sk-|ghp_|github_pat_|xox[baprs]-)[a-z0-9_\-]{8,}"
    r"|AKIA[0-9A-Z]{16}|(?:api[_ -]?key|access[_ -]?token|password|authorization|client[_ -]?secret)"
    r"""\s*["']?\s*[:=]\s*["']?[^\s"',;}{]{4,})"""
)


def _clean(value):
    if isinstance(value, str):
        if "PRIVATE KEY-----" in value:
            return "[redacted]"
        return SECRET.sub("[redacted]", value)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    return value


def _display(value):
    if isinstance(value, list):
        return [projected for item in value if (projected := _display(item)) is not None]
    if isinstance(value, dict):
        if any(str(value.get(key, "")).lower() in PRIVATE_BLOCKS for key in ("role", "type", "kind")):
            return None
        return {key: _display(item) for key, item in value.items() if key in DISPLAY_FIELDS}
    return _clean(value)


def _metadata(record):
    result = {}
    for key, value in record.items():
        if key not in METADATA_FIELDS:
            continue
        if key in REF_FIELDS:
            if isinstance(value, dict):
                result[key] = {k: _clean(value[k]) for k in ("source_root", "path", "locator", "sha256")
                               if k in value}
        elif isinstance(value, list):
            result[key] = [_clean(item) for item in value if not isinstance(item, (dict, list))]
        elif not isinstance(value, dict):
            result[key] = _clean(value)
    return result


def _allowed_payload(batch_dir, run_dir, ref, sha256, event):
    if not isinstance(ref, dict) or ref.get("sha256") != sha256 or ref["payload_layer"] not in VISIBLE_LAYERS:
        raise ValueError("approved payload hash differs from indexed source")
    relative = Path(ref["path"])
    root_type = ref.get("source_root")
    if root_type == "run" and relative.parts[:1] != ("raw",):
        raise ValueError("approved payload must come from a run's raw archive")
    if root_type == "batch" and relative.parts[0] not in {
        "native", "checkpoints", "traces", "claude_code_conversations", "codex_conversations",
    }:
        raise ValueError("approved payload must come from native events or checkpoint interactions")
    if relative.suffix not in {".json", ".jsonl", ".gz"} or any(
        part.startswith(".") or part in {"home", "work", "workspace", "scorer", "private", "snapshots"}
        for part in relative.parts
    ):
        raise ValueError("payload outside the allowed display evidence")
    locator = ref.get("locator") or {"type": "file"}
    if relative.suffix == ".gz" and not OFFICIAL_DISPLAY.fullmatch(locator.get("pointer", "")):
        raise ValueError("only scoped official interactions are display evidence")
    value = read_ref(batch_dir, run_dir, ref)
    if isinstance(value, str) and event.get("type") in {"model_response", "user_message", "task_query"}:
        return _clean(value)
    if not isinstance(value, (dict, list)):
        raise ValueError("native display evidence must be structured JSON")
    return _display(value)


def _documents(path, data):
    if path.endswith(".jsonl"):
        yield from (json.loads(line) for line in data.splitlines() if line.strip())
    elif path == "trajectory.ctx.json":
        for trajectory in json.loads(data)["trajectories"]:
            yield _metadata(trajectory)
            for segment in trajectory["segments"]:
                yield _metadata(segment)
                yield from map(_metadata, segment["steps"])
    else:
        yield json.loads(data)


def _windows(run_id, records, budget):
    windows = []
    content = []
    event_ids = set()
    size = 0
    for record in records:
        raw = encode(record)
        offset = 0
        while offset < len(raw):
            if size and size + len(raw) - offset + 1 > budget:
                windows.append({"run_id": run_id, "records": "\n".join(content), "event_ids": sorted(event_ids)})
                content, size = [], 0
                event_ids.clear()
            piece = raw[offset:offset + budget - 1].decode(errors="ignore")
            content.append(piece)
            if record["source"] == "events.jsonl" and record["data"].get("event_id"):
                event_ids.add(record["data"]["event_id"])
            width = len(piece.encode())
            size += width + 1
            offset += width
    if content:
        windows.append({"run_id": run_id, "records": "\n".join(content), "event_ids": sorted(event_ids)})
    ids = [f"{run_id}:{index}" for index in range(len(windows))]
    for index, window in enumerate(windows):
        window["window_id"] = ids[index]
        window["previous_window"] = ids[index - 1] if index else None
        window["next_window"] = ids[index + 1] if index + 1 < len(ids) else None
    return windows


def build_inputs(archive, batch_dir, spec):
    groups = spec["development_groups"]
    holdout = spec["holdout_groups"]
    if not isinstance(groups, dict) or not groups or not isinstance(holdout, list):
        raise ValueError("development_groups and holdout_groups must be explicitly frozen")
    declared_groups = {*groups, *holdout}
    for group in declared_groups:
        identifier(group)
    if set(groups) & set(holdout) or len(holdout) != len(set(holdout)):
        raise ValueError("development and holdout groups must be disjoint")
    run_groups = {}
    for group, run_ids in groups.items():
        if not isinstance(run_ids, list) or not run_ids:
            raise ValueError(f"development group {group} must contain explicit run IDs")
        for run_id in run_ids:
            if identifier(run_id) in run_groups:
                raise ValueError(f"run listed more than once: {run_id}")
            run_groups[run_id] = group
    approvals = {}
    for item in spec.get("payload_allowlist", []):
        key = (item["run_id"], item["event_id"], item["field"])
        if (key in approvals or key[0] not in run_groups or key[2] not in REF_FIELDS
                or not re.fullmatch("[a-f0-9]{64}", item["sha256"])):
            raise ValueError("invalid or duplicate payload approval")
        approvals[key] = item["sha256"]
    budget = spec.get("window_bytes", 12_000)
    if type(budget) is not int or budget < 512:
        raise ValueError("window_bytes must be an integer of at least 512")

    windows, refs, errors = [], [], []
    source_events = set()
    executor_model_ids = set()
    run_context = {}
    batch_index = inside(archive, "batch_index.json").read_bytes()
    batch_rows = json.loads(batch_index)
    for run_id, group in run_groups.items():
        run_dir, ref_by_path, artifacts = indexed_run(archive, run_id, batch_rows)
        records = []
        files = (*INDEX_FILES, "scores.jsonl") if spec.get("allow_offline_scores") is True else INDEX_FILES
        for path in files:
            if path not in ref_by_path and path != "artifacts.json":
                errors.append({"run_id": run_id, "path": path, "reason": "not_exposed"})
                continue
            data = artifacts if path == "artifacts.json" else checked_bytes(run_dir, path, ref_by_path[path])
            refs.append({"run_id": run_id, "path": path, "sha256": digest(data),
                         "bytes": len(data), "development_group": group})
            for index, doc in enumerate(_documents(path, data)):
                row = _metadata(doc)
                if path == "manifest.json":
                    if doc.get("run_id") != run_id:
                        raise ValueError(f"manifest/run identity mismatch for {run_id}")
                    if doc.get("model_id"):
                        executor_model_ids.add(str(doc["model_id"]))
                    run_context[run_id] = {key: doc.get(key) for key in ("task_name", "schedule", "seed", "job_attempt_id")}
                if path == "scores.jsonl":
                    row.update({key: doc[key] for key in ("raw_score", "score", "reward")
                                if type(doc.get(key)) in (int, float)})
                    row["visibility"] = "offline_score"
                if path == "events.jsonl" and doc.get("event_id"):
                    event_id = doc["event_id"]
                    source_events.add((run_id, event_id))
                    payloads = {}
                    for field in REF_FIELDS:
                        key = (run_id, event_id, field)
                        if key in approvals:
                            if doc.get("teacher_visible") is False or doc.get("type") == "native_private_block":
                                raise ValueError("native event is excluded from teacher evidence")
                            approved = approvals.pop(key)
                            payloads[field] = _allowed_payload(batch_dir, run_dir, doc.get(field), approved, doc)
                    if payloads:
                        row["approved_display_payloads"] = payloads
                if row:
                    records.append({"source": path, "record_index": index, "data": row})
        windows.extend(_windows(run_id, records, budget))
    if approvals:
        raise ValueError("approved payload references an unknown event")
    index = {
        "schema_version": SCHEMA, "source_run_refs": list(run_groups),
        "input_hashes": refs, "batch_index_sha256": digest(batch_index),
        "input_allowlist": spec.get("payload_allowlist", []),
        "private_eval_exclusions": PRIVATE_EXCLUSIONS,
        "split_provenance": {"development_groups": groups, "holdout_groups": holdout},
        "offline_scores_allowed": spec.get("allow_offline_scores") is True,
        "executor_model_ids": sorted(executor_model_ids), "reading_coverage": [],
        "run_context": run_context,
        "missing_reasons": errors, "projection": "indexed_metadata_and_explicit_display_payloads",
        "windows": [
            {key: value for key, value in window.items() if key != "records"}
            | {"bytes": len(window["records"].encode()), "sha256": digest(window["records"].encode())}
            for window in windows
        ],
        "unread_refs": [window["window_id"] for window in windows],
    }
    return index, windows, source_events
