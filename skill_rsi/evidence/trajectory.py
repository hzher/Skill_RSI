# Adapted from Skill_RSI_hzh/evidence/trajectory.py (MIT).
"""Session segmentation, visible input changes and source-backed context labels."""

from collections import defaultdict

from .io import read_ref
from .schema import GENERATOR, SCHEMA, event, identifier


def _shape(archive, ref):
    if not ref:
        return None
    try:
        payload = read_ref(archive.root, None, ref)
    except (FileNotFoundError, IndexError, KeyError, TypeError, ValueError):
        return None
    if isinstance(payload, list):
        messages, settings = payload, {}
    elif isinstance(payload, dict):
        messages = payload.get("messages", payload.get("input"))
        settings = {key: value for key, value in payload.items() if key not in {"messages", "input"}}
    else:
        return None
    if not isinstance(messages, list):
        return None
    system = [item for item in messages if isinstance(item, dict) and item.get("role") == "system"]
    prefix = [identifier(item) for item in messages if not isinstance(item, dict) or item.get("role") != "system"]
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


def _numeric_order(rows):
    return all(isinstance(row.get("turn_id"), int) and isinstance(row.get("native_step_id"), int)
               and row.get("purpose") in {None, "main", "chat", "generate"} for row in rows) and len({
                   (row["turn_id"], row["native_step_id"]) for row in rows
               }) == len(rows)


def _order(rows, events):
    if _numeric_order(rows):
        return sorted(rows, key=lambda item: (item["turn_id"], item["native_step_id"])), True
    starts = [events.get(row.get("event_id"), {}) for row in rows]
    coordinates = [(row.get("source_id"), row.get("seq")) for row in starts]
    same_source = len({source for source, _ in coordinates}) == 1
    comparable = all(source is not None and type(seq) is int for source, seq in coordinates)
    if same_source and comparable and len(set(coordinates)) == len(rows):
        return [row for _, row in sorted(zip(coordinates, rows, strict=True), key=lambda entry: entry[0][1])], True
    return rows, False


def build_trajectory(archive, run, run_id):
    groups = defaultdict(list)
    session_refs = defaultdict(dict)
    session_events = defaultdict(list)
    parents = defaultdict(set)
    indexed_events = {row["event_id"]: row for row in run.events}
    for row in run.events:
        if row.get("native_session_id") or row.get("thread_id"):
            key = _trajectory_key(row)
            ref = row["raw_ref"]
            session_refs[key].setdefault(ref["path"], ref)
            session_events[key].append(row["event_id"])
            groups[key]
            if row.get("parent_thread_id"):
                parents[key].add(row["parent_thread_id"])
    for row in run.requests:
        groups[_trajectory_key(row)].append(row)
    trajectories = []
    trajectories_by_thread = defaultdict(list)
    for key in groups:
        if key[2]:
            trajectories_by_thread[key[2]].append(identifier(run_id, key))
    for key, rows in groups.items():
        runtime, session, thread = key
        trajectory_id = identifier(run_id, key)
        parent_ids = {item for native_id in parents[key] for item in trajectories_by_thread[native_id]
                      if item != trajectory_id}
        parent = next(iter(parent_ids)) if len(parent_ids) == 1 else None
        trajectory = {
            "trajectory_id": trajectory_id, "agent_path": None, "native_session_id": session,
            "thread_id": thread, "runtime_id": runtime, "parent_trajectory_id": parent,
            "source_refs": list(session_refs[key].values()), "segments": [],
            "observed_event_ids": session_events[key],
            "missing_reasons": ([] if parent else ["native_agent_relationship_unverified"])
            + (["native_session_unresolved"] if not (session or thread) else []),
        }
        trajectories.append(trajectory)
        rows, ordered = _order(rows, indexed_events)
        if not ordered and rows:
            trajectory["missing_reasons"].append("call_causal_order_unverified")
        previous = None
        for row in rows:
            request, actual = row.get("request_ref"), row.get("actual_context_ref")
            logical_input = row.get("logical_input_ref")
            observed = actual or logical_input or (request if (request or {}).get("payload_layer") == "native_options" else None)
            shape = _shape(archive, observed)
            kind = _change(previous[1], shape) if ordered and previous and previous[2] and observed and (
                previous[2].get("payload_layer") == observed.get("payload_layer")
                and previous[0].get("purpose") == row.get("purpose")
            ) else "unknown"
            if previous and kind not in {"unknown", "unchanged"} and observed:
                record = event(observed, "context_change", len(run.events) + 1, actor_type="harness",
                               native_session_id=session, thread_id=thread, runtime_id=runtime,
                               before_request_ref=previous[2], after_request_ref=observed,
                               change_kind=kind, change_evidence="observed_" + observed["payload_layer"],
                               application_status="unknown")
                record["source_id"] = f"context_delta:{record['source_id']}"
                run.events.append(record)
                row.setdefault("source_event_ids", []).append(record["event_id"])
            segments = trajectory["segments"]
            split = previous and row.get("applied_change_kind") in {"truncate", "reorder", "non_append", "compaction"}
            if split or not segments:
                segments.append({
                    "segment_id": identifier(trajectory_id, len(segments)),
                    "parent_segment_id": segments[-1]["segment_id"] if segments else None,
                    "branch": "continuation" if segments else "fork" if parent else "root",
                    "input_context_ref": actual or logical_input,
                    "capture_status": "verified" if actual else "observed" if logical_input else "insufficient",
                    "context_visibility": "effective_input" if actual else (
                        "logical_input" if logical_input else row.get("request_evidence_kind", "unknown")
                    ),
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
            previous = row, shape, observed
        if not rows and session_events[key]:
            trajectory["segments"].append({
                "segment_id": identifier(trajectory_id, 0), "parent_segment_id": None,
                "branch": "fork" if parent else "root", "input_context_ref": None, "capture_status": "insufficient",
                "context_visibility": "unknown", "source_event_ids": session_events[key], "steps": [],
            })
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
                basis = "inferred" if str(row.get("change_evidence") or "").startswith("observed_") else "insufficient"
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
