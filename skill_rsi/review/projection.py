"""Display projection for source-backed approval and discovery."""

# Adapted from Skill_RSI_hzh/skill_discovery/inputs.py (MIT).
import json
import re
from pathlib import Path

from ..evidence.io import read_ref


INDEX_FILES = (
    "manifest.json", "events.jsonl", "requests/index.jsonl", "trajectory.ctx.json",
    "context_episodes.jsonl", "artifacts.json", "quality.json", "observations.json",
)
PRIVATE_EXCLUSIONS = [
    "official environment data", "hidden tests and reference answers", "scorer private details",
    "holdout runs", "credentials", "private reasoning",
]
REF_FIELDS = {
    "raw_ref", "request_ref", "actual_context_ref", "response_ref", "readback_ref",
    "validated_action_ref", "schema_ref", "source_ref", "rendered_result_ref", "initial_state_ref",
    "logical_input_ref",
}
REFERENCE_METADATA = REF_FIELDS | {"boundary_ref", "evidence_ref", "visible_state_ref"}
VISIBLE_LAYERS = {
    "native_capture", "native_events", "native_rpc", "native_options", "native_output_chunks",
    "native_action", "native_usage", "task_query", "task_schema", "official_action", "environment_observation",
    "tool_result", "model_input", "model_output", "transport_request", "transport_response",
    "sdk_options", "logical_request", "trace_payload", "codex_trace_payload", "tool_output",
    "native_trace", "codex_raw_payload", "codex_logical_input", "claude_messages", "claude_transcript",
    "claude_native_transcript", "codex_rollout", "hook_event", "native_session", "native_cli",
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
    project_id lineage_id native_run_id source_conflict operation_key decision_id
    opportunity_id stage layer observation artifact_version match_method policy_version eligible
    boundary_event_id diagnosis close_status missing_reason subject identity_basis logical_input_status
    source_event_id mode component_id version binding_revision artifact_digest native_response_prepared
    scorer_version direction unit higher_is_better native_success
    label_confidence trigger_status application_status recording_closed diagnostics
""".split()) | REFERENCE_METADATA
DISPLAY_FIELDS = set("""
    role type kind name message messages text content systemPrompt system_prompt system
    input output result tool_calls tool_use tool_result function callId toolCallId
    arguments args action tool_name error records item response blocks
    query feedback actual_prompt prompt file_path path operation old_string new_string
    command edit summary instruction prompt_text allowed_feedback data event payload chunk delta
    native_record tool_input tool_response stdout stderr content_blocks
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


def metadata(record):
    result = {}
    for key, value in record.items():
        if key not in METADATA_FIELDS:
            continue
        if key in REFERENCE_METADATA:
            if isinstance(value, dict):
                result[key] = {k: _clean(value[k]) for k in ("source_root", "path", "locator", "sha256", "bytes", "payload_layer")
                               if k in value}
        elif key == "visibility" and isinstance(value, dict):
            statuses = {"observed", "observed_subset", "verified", "unknown", "not_applicable", "unavailable"}
            result[key] = {name: status for name, status in value.items()
                           if name in {"native_record", "model_input", "tool_output", "transport"}
                           and isinstance(status, str) and status in statuses}
        elif isinstance(value, list):
            result[key] = [_clean(item) for item in value if not isinstance(item, (dict, list))]
        elif not isinstance(value, dict):
            result[key] = _clean(value)
    return result


def allowed_payload(batch_dir, run_dir, ref, sha256, event, *, training=False):
    if not isinstance(ref, dict) or ref.get("sha256") != sha256 or ref.get("payload_layer") not in VISIBLE_LAYERS:
        raise ValueError("Approved payload has an unapproved layer or different hash")
    if event.get("teacher_visible") is False or event.get("type") == "native_private_block":
        raise ValueError("Native event is excluded from discovery evidence")
    if any(str(event.get(key, "")).casefold() in PRIVATE_BLOCKS for key in ("role", "type", "kind")):
        raise ValueError("Native event contains private content")
    relative = Path(ref["path"])
    root_type = ref.get("source_root")
    if root_type == "run" and relative.parts[:1] != ("raw",):
        raise ValueError("approved payload must come from a run's raw archive")
    if root_type not in {"run", "batch"}:
        raise ValueError("Unrecognized payload root")
    if root_type == "batch" and relative.parts[0] not in {
        "native", "checkpoints", "traces", "claude_code_conversations", "codex_conversations",
        "payloads", "trace.jsonl",
    } and relative.suffix not in {".jsonl", ".ndjson"}:
        raise ValueError("Approved payload must come from a selected conversation or native interaction")
    if relative.suffix not in {".json", ".jsonl", ".ndjson", ".gz"} or any(
        part.startswith(".") or part in {
            "home", "work", "workspace", "scorer", "private", "snapshots", "credentials",
            "auth.json", "evaluator", "evaluator-data", "private-tests", "reference-answers",
        }
        for part in map(str.casefold, relative.parts)
    ):
        raise ValueError("payload outside the allowed display evidence")
    locator = ref.get("locator") or {"type": "file"}
    if relative.suffix == ".gz" and not OFFICIAL_DISPLAY.fullmatch(locator.get("pointer", "")):
        raise ValueError("only scoped official interactions are display evidence")
    value = read_ref(batch_dir, run_dir, ref)
    if isinstance(value, str) and event.get("type") in {"model_response", "user_message", "task_query", "tool_result"}:
        return _clean(value)
    if not isinstance(value, (dict, list)):
        raise ValueError("Native display evidence must be structured JSON")
    if training:
        return _training_state(value)
    return _display(value)



_TRAINING_KEYS = {
    "query", "feedback", "memory", "checkpoint_ref", "instance_id", "instance_index", "seed", "host", "host_version",
    "environment_version", "task_id", "instance_order", "boundary_ref",
}
_REF_KEYS = {
    "type", "source_root", "path", "locator", "bytes", "sha256", "payload_layer", "run_id",
    "event_id", "version", "pointer", "line", "line_pointer", "artifact_id",
}
_MEMORY_KEY = re.compile(r"[A-Za-z0-9_-]{1,48}\Z")
_PRIVATE_FIELD = re.compile(r"(?i)(private|secret|credential|answer|grader|password|token|thinking|reasoning)")


def _state_ref(value):
    if not isinstance(value, dict):
        return None
    reference = {key: _clean(item) for key, item in value.items() if key in _REF_KEYS and not isinstance(item, (dict, list))}
    if isinstance(value.get("locator"), dict):
        reference["locator"] = {k: v for k, v in value["locator"].items() if k in {"type", "line", "pointer", "line_pointer"}}
    location = reference.get("path")
    if location is not None and (not isinstance(location, str) or not location or Path(location).is_absolute()
                                 or ".." in Path(location).parts or _PRIVATE_FIELD.search(location)):
        return None
    return reference or None


def _training_state(value):
    if not isinstance(value, dict):
        return None
    state = value.get("visible_state", value)
    if not isinstance(state, dict):
        return None
    result = {}
    for key, item in state.items():
        if key not in _TRAINING_KEYS:
            continue
        if key == "feedback":
            if isinstance(item, list) and all(isinstance(text, str) for text in item):
                result[key] = [_clean(text) for text in item]
        elif key == "memory":
            if isinstance(item, dict) and all(_MEMORY_KEY.fullmatch(name) and isinstance(text, str)
                                              for name, text in item.items()):
                result[key] = {name: _clean(text) for name, text in item.items() if not _PRIVATE_FIELD.search(name)}
        elif key in {"checkpoint_ref", "boundary_ref"}:
            if reference := _state_ref(item):
                result[key] = reference
        elif key == "instance_order":
            if isinstance(item, list) and all(type(value) in {str, int} for value in item):
                result[key] = _clean(item)
        elif type(item) in {str, int}:
            result[key] = _clean(item)
    return result


def documents(path, data):
    if path.endswith(".jsonl"):
        yield from (json.loads(line) for line in data.splitlines() if line.strip())
    elif path == "trajectory.ctx.json":
        for trajectory in json.loads(data)["trajectories"]:
            yield trajectory
            for segment in trajectory["segments"]:
                yield segment
                yield from segment["steps"]
    elif path == "observations.json":
        value = json.loads(data)
        for name in ("opportunities", "content_flow", "source_health", "diagnostics"):
            for row in value.get(name, ()):
                yield {"type": name, **row}
        for operation in value.get("operations", ()):
            for row in operation["receipts"]:
                yield {"type": "operation_receipt", "operation_key": operation["operation_key"], **row}
    else:
        yield json.loads(data)

