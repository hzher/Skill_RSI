"""Session segmentation, visible input changes and source-backed context labels."""

from collections import defaultdict
from .schema import GENERATOR, SCHEMA, event, identifier


def _shape(archive, ref):
    if not ref or ref.get("payload_layer") != "native_options" or ref["locator"]["type"] != "file":
        return None
    payload = archive.json(ref["path"])
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    system = [item for item in messages if isinstance(item, dict) and item.get("role") == "system"]
    prefix = [identifier(item) for item in messages if not isinstance(item, dict) or item.get("role") != "system"]
    settings = {key: value for key, value in payload.items() if key != "messages"}
    return prefix, identifier(system, settings)


def _change(before, after):
    if before is None or after is None:
        return "unknown"
    old, new = before[0], after[0]
    if old == new:
        return "unchanged" if before[1] == after[1] else "system_tools_or_config_changed"
    if new[:len(old)] == old:
        return "append" if before[1] == after[1] else "append_and_config_changed"
    if old[:len(new)] == new:
        return "truncate"
    if sorted(old) == sorted(new):
        return "reorder"
    return "non_append"


def _trajectory_key(item):
    return item.get("runtime_id"), item.get("native_session_id"), item.get("thread_id")


def _call_order(rows):
    return all(isinstance(row.get("turn_id"), int) and isinstance(row.get("native_step_id"), int)
               and row.get("purpose") in {None, "main", "chat", "generate"} for row in rows) and len({
                   (row["turn_id"], row["native_step_id"]) for row in rows
               }) == len(rows)


def build_trajectory(archive, run, run_id):
    groups = defaultdict(list)
    session_refs = defaultdict(dict)
    session_events = defaultdict(list)
    for row in run.events:
        if row.get("native_session_id") or row.get("thread_id"):
            key = _trajectory_key(row)
            ref = row["raw_ref"]
            session_refs[key].setdefault(ref["path"], ref)
            session_events[key].append(row["event_id"])
            groups[key]
    for row in run.requests:
        groups[_trajectory_key(row)].append(row)
    trajectories = []
    for key, rows in groups.items():
        runtime, session, thread = key
        trajectory_id = identifier(run_id, key)
        trajectory = {
            "trajectory_id": trajectory_id, "agent_path": None, "native_session_id": session,
            "thread_id": thread, "runtime_id": runtime, "parent_trajectory_id": None,
            "source_refs": list(session_refs[key].values()), "segments": [],
            "observed_event_ids": session_events[key],
            "missing_reasons": ["native_agent_relationship_unverified"]
            + (["native_session_unresolved"] if not (session or thread) else []),
        }
        trajectories.append(trajectory)
        ordered = _call_order(rows)
        if ordered:
            rows = sorted(rows, key=lambda item: (item["turn_id"], item["native_step_id"]))
        elif rows:
            trajectory["missing_reasons"].append("call_causal_order_unverified")
        previous = None
        for row in rows:
            request, actual = row.get("request_ref"), row.get("actual_context_ref")
            shape = _shape(archive, request)
            kind = _change(previous[1], shape) if ordered and previous else "unknown"
            if previous and kind not in {"unknown", "unchanged"} and request:
                record = event(request, "context_change", 1, actor_type="harness",
                               native_session_id=session, thread_id=thread, runtime_id=runtime,
                               before_request_ref=previous[0].get("request_ref"), after_request_ref=request,
                               change_kind=kind, change_evidence="observed_native_options",
                               application_status="unknown")
                record["source_id"] = f"context_delta:{record['source_id']}"
                run.events.append(record)
                row.setdefault("source_event_ids", []).append(record["event_id"])
            segments = trajectory["segments"]
            split = previous and actual and previous[0].get("actual_context_ref") and (
                row.get("applied_change_kind") in {"truncate", "reorder", "non_append", "compaction"}
            )
            if split or not segments:
                segments.append({
                    "segment_id": identifier(trajectory_id, len(segments)),
                    "parent_segment_id": segments[-1]["segment_id"] if segments else None,
                    "branch": "continuation" if segments else "root", "input_context_ref": actual,
                    "capture_status": "verified" if actual else "insufficient",
                    "context_visibility": "effective_input" if actual else row.get("request_evidence_kind", "unknown"),
                    "change_kind": row.get("applied_change_kind") if split else "initial",
                    "change_evidence": row.get("applied_change_evidence") if split else None,
                    "before_request_ref": previous[0].get("request_ref") if split else None, "after_request_ref": request,
                    "source_event_ids": row.get("source_event_ids", []), "steps": [],
                })
            segment = segments[-1]
            segment["steps"].append({
                **row, "step_id": identifier(segment["segment_id"], len(segment["steps"])),
                "step_unit": "transport_attempt" if row.get("attempt_id") else "native_call",
                "actual_context_ref": actual, "request_ref": request,
                "response_ref": row.get("response_ref"), "state_effect": row.get("state_effect", "unknown"),
                "instance_id": row.get("instance_id"), "instance_index": row.get("instance_index"),
                "logical_call_id": row.get("logical_call_id"), "attempt_id": row.get("attempt_id"),
                "tool_refs": row.get("tool_refs", []), "usage_refs": row.get("usage_refs", []),
                "missing_reasons": row.get("missing_reasons", []),
            })
            previous = row, shape
    return {"schema_version": SCHEMA, "generator_version": GENERATOR, "run_id": run_id,
            "trajectories": trajectories}


def context_episodes(run, run_id):
    episodes = []
    versions = defaultdict(list)
    for version in run.versions:
        for event_id in version.get("source_event_ids", []):
            versions[event_id].append(version["version_id"])
    for row in run.events:
        kind = row["type"]
        native_type = str(row.get("native_type") or "")
        tags = []
        basis = "observed"
        if kind == "memory_operation" and row.get("operation") and native_type == "memory_after":
            tags.append("offload_recall")
            if row["operation"] in {"write", "update", "delete", "rename"}:
                tags.append("state_update")
        elif kind == "context_change":
            if row.get("change_kind") in {"append", "append_and_config_changed"}:
                tags.append("state_retention")
                basis = "inferred"
            else:
                tags.append("context_edit")
                basis = "inferred" if row.get("change_evidence") == "observed_native_options" else "insufficient"
        elif kind == "native_retry" or "budget" in native_type.lower():
            tags.append("budget_efficiency")
        elif kind == "tool_call" and row.get("representation") != "rpc" and (
            row.get("memory_scoped") or str(row.get("tool_name") or "").startswith("memory/")
        ):
            tags.append("offload_recall")
            basis = "inferred"
        elif kind == "skill_load":
            row["label_status"] = "insufficient"
        if not tags:
            if (kind == "context_change" or native_type == "memory_rejected") and row.get("change_kind") != "append":
                row["label_status"] = "insufficient"
            continue
        context_id = identifier(run_id, row["event_id"], "context")
        facts = [row["event_id"], *row.get("source_event_ids", [])]
        row["label_status"] = "annotated"
        row["context_episode_id"] = context_id
        applied_id = row.get("applied_version_id")
        episodes.append({
            "context_episode_id": context_id, "behavior_tags": tags, "source_event_ids": facts,
            "instance_id": row.get("instance_id"), "instance_index": row.get("instance_index"),
            "evidence_status": basis, "continued_event_ids": [],
            "operation_ids": [row["operation_id"]] if row.get("operation_id") else [],
            "artifact_version_refs": [version for fact in facts for version in versions[fact]],
            "before_request_ref": row.get("before_request_ref"), "after_request_ref": row.get("after_request_ref"),
            "application_status": row.get("application_status", "unknown"),
            "downstream_status": "pending", "missing_reasons": ["adoption_and_effect_unverified"],
            "label_id": context_id, "label_scope": "source_event", "label_method": "native_event_rules",
            "label_version": GENERATOR, "label_evidence_refs": [row["raw_ref"]],
            "label_confidence": "high" if basis == "observed" else "limited", "annotator_model": None,
            "lifecycle_refs": [{"stage": "applied", "source_event_ids": facts, "artifact_ref": applied_id}]
            if applied_id else [],
        })
    return episodes
