"""Extract decision opportunities and compare source-backed observation boundaries."""

from collections import defaultdict

from ..contracts.events import observation_counts
from .io import at_pointer, read_ref
from .schema import identifier

_OPPORTUNITY_TYPES = {
    "session_resume": "resume",
    "resume": "resume",
    "session_start": "session_start",
    "before_action": "before_action",
    "compaction_installed": "post_compaction",
    "constraint_use": "constraint_use",
    "tool_result": "tool_result",
    "memory_opportunity": "memory_use",
    "decision_state": "memory_use",
    "training_state": "memory_use",
}
_PROBE_FIELDS = {
    "raw_ref", "source_ref", "rendered_result_ref", "request_ref", "logical_input_ref",
    "actual_context_ref", "state_ref", "after_request_ref", "before_request_ref",
    "validated_action_ref", "readback_ref", "response_ref",
}


def _opportunity(run, event, kind):
    return {
        "opportunity_id": identifier(run.identity.get("run_id"), event["event_id"], kind),
        "boundary_event_id": event["event_id"], "boundary_ref": event["raw_ref"], "kind": kind,
        "thread_id": event.get("thread_id"), "native_session_id": event.get("native_session_id"),
        "target_object": event.get("target_object"), "visible_state_ref": event.get("visible_state_ref"),
        "context_window_id": event.get("context_window_id"), "eligible": event.get("eligible"),
        "policy_version": event.get("policy_version"), "prediction": event.get("prediction"),
        "decision_id": event.get("decision_id"), "source_id": event.get("source_id"),
    }


def _call_links(run, opportunities):
    """Join only explicit native output IDs within the recorded thread and call order."""
    events = {row["event_id"]: row for row in run.events}
    main_calls = defaultdict(list)
    for request in run.requests:
        started = events.get(request.get("event_id"), {})
        if request.get("purpose") == "main" and started.get("seq") is not None:
            main_calls[request.get("thread_id")].append((started["seq"], request))
    for calls in main_calls.values():
        calls.sort(key=lambda item: item[0])
    for item in opportunities:
        if item["kind"] != "tool_result":
            continue
        event = events[item["boundary_event_id"]]
        tool = event.get("tool_call_id")
        thread, seq = event.get("thread_id"), event.get("seq")
        rendered = event.get("rendered_result_ref")
        yield {
            "opportunity_id": item["opportunity_id"], "stage": "tool_return", "layer": "native_tool",
            "artifact_version": rendered.get("sha256") if rendered else None, "call_ref": tool,
            "match_method": "native_tool_receipt",
            "observation": "present" if rendered else "unknown",
            "evidence_ref": rendered or item["boundary_ref"], "subject": "associated_tool_output",
            "missing_reason": None if rendered else "missing_tool_output_payload",
        }
        following = next((req for offset, req in main_calls.get(thread, ()) if seq is not None and offset > seq), {})
        request_ref = following.get("logical_input_ref") or following.get("actual_context_ref")
        links = following.get("tool_outputs_in_logical_input") or ()
        linked = next((edge for edge in links if tool and edge.get("tool_call_id") == tool
                       and edge.get("mapping_status") == "unique"), None)
        yield {
            "opportunity_id": item["opportunity_id"], "stage": "main_input",
            "layer": "logical_request", "call_ref": following.get("logical_call_id"), "tool_call_id": tool,
            "item_id": linked.get("input_item_id") if linked else None,
            "artifact_version": None, "match_method": "native_item_link",
            "observation": "present" if thread and request_ref and linked else "unknown",
            "evidence_ref": request_ref, "subject": "associated_tool_output",
            "missing_reason": None if thread and request_ref and linked else "unverified_tool_output_link",
        }


def _probe_boundary(archive, lookups, check, expected):
    row = lookups.get(check.get("ref_id"))
    field = check.get("field")
    if field not in _PROBE_FIELDS:
        raise ValueError(f"Unsupported probe reference field: {field!r}")
    ref = row.get(field) if row else None
    stage = check["stage"]
    if stage == "main_input" and (
        not row or row.get("purpose") != "main" or field not in {"logical_input_ref", "actual_context_ref"}
    ):
        ref = None
    observation, reason = "unknown", "missing_observation_payload"
    if ref:
        try:
            value = read_ref(archive.root, None, ref)
            observed_value = at_pointer(value, check.get("json_pointer", ""))
            known_complete = check.get("complete") is True and not row.get("missing_reasons")
            observation, reason = ("present", None) if type(observed_value) is type(expected) and observed_value == expected else (
                ("absent", None) if known_complete else ("unknown", "incomplete_comparison_scope")
            )
        except (IndexError, KeyError, TypeError):
            if check.get("complete") is True and not row.get("missing_reasons"):
                observation, reason = "absent", None
            else:
                reason = "unresolved_structured_field"
        except (FileNotFoundError, OSError, ValueError):
            reason = "unavailable_or_invalid_observation_payload"
    return {
        "stage": stage, "layer": ref.get("payload_layer") if ref else None,
        "call_ref": row.get("logical_call_id") if row else None,
        "artifact_version": check.get("artifact_version"), "match_method": "exact_structured_field",
        "observation": observation, "evidence_ref": ref, "missing_reason": reason,
    }


def _verdict(kind, flow):
    by_stage = {row["stage"]: row["observation"] for row in flow}
    boundaries = {
        "stale_resume": ("confirmed", "readback"),
        "truncated_tool_output": ("tool_return", "history"),
        "compaction_loss": ("compaction_before", "compaction_installed"),
    }
    if kind not in boundaries:
        raise ValueError(f"Unsupported fact probe diagnosis: {kind}")
    before, after = boundaries[kind]
    if by_stage.get(before) == "present" and by_stage.get(after) == "absent":
        outcome = "observed_version_change" if kind == "stale_resume" else "observed_exact_field_loss"
        if kind == "stale_resume" and by_stage.get("stale_version_readback") != "present":
            outcome = "current_version_missing_from_readback"
        return outcome
    if by_stage.get(after) == "present":
        return "expected_field_retained"
    return "unknown"


def derive_observations(archive, run, probes=()):
    result = dict(run.observations)
    opportunities = list(result.get("opportunities", ()))
    observed_events = {row.get("boundary_event_id") for row in opportunities}
    for event in run.events:
        native = str(event.get("native_type") or "")
        kind = _OPPORTUNITY_TYPES.get(event["type"])
        if not kind and "compact" in native and "install" in native:
            kind = "post_compaction"
        if kind and event["event_id"] not in observed_events:
            opportunities.append(_opportunity(run, event, kind))
    content_flow = [*result.get("content_flow", ()), *_call_links(run, opportunities)]
    findings = list(result.get("diagnostics", ()))
    lookups = {row["event_id"]: row for row in run.events}
    for row in run.requests:
        for key in ("request_id", "logical_call_id"):
            if row.get(key):
                lookups.setdefault(row[key], row)
    for probe in probes:
        event_id = probe["boundary_event_id"]
        if event_id not in lookups or "raw_ref" not in lookups[event_id]:
            continue
        kind = probe["diagnosis"]
        item = _opportunity(run, lookups[event_id], kind)
        item["target_object"] = probe.get("target_object")
        item["opportunity_id"] = identifier(item["opportunity_id"], probe.get("probe_id"), item["target_object"])
        if any(previous["opportunity_id"] == item["opportunity_id"] for previous in opportunities):
            raise ValueError("Duplicate fact probe at the same decision opportunity")
        opportunities.append(item)
        flow = [
            {"opportunity_id": item["opportunity_id"], **_probe_boundary(
                archive, lookups, check, check["expected"] if "expected" in check else probe["expected"]
            )}
            for check in probe["checks"]
        ]
        content_flow.extend(flow)
        findings.append({
            "opportunity_id": item["opportunity_id"], "diagnosis": kind, "boundary_ref": item["boundary_ref"],
            "target_object": item["target_object"], "status": _verdict(kind, flow),
            "main_input_observation": next(
                (row["observation"] for row in flow if row["stage"] == "main_input"), "unknown"
            ),
            "comparison_scope": "exact_field_only",
        })
    health = list(result.get("source_health", ()))
    identities = {row.get("source_id") for row in health}
    for name, scope in (run.identity.get("runtime_health") or {}).items():
        if name not in identities:
            health.append({
                "source_id": name, "capture_profile": "native", "start_cursor": scope.get("start_cursor"),
                "end_cursor": scope.get("end_cursor"), "sampling_rule": scope.get("sampling_rule"),
                "gap_ranges": scope.get("gap_ranges"), "dropped_count": scope.get("dropped_count"),
                "close_status": "closed" if scope.get("recording_closed") is True else (
                    "open_or_incomplete" if scope.get("recording_closed") is False else "unknown"
                ),
            })
    if opportunities:
        result["opportunities"] = opportunities
    if content_flow:
        result["content_flow"] = content_flow
        result["coverage"] = {
            stage: observation_counts(
                (item for item in content_flow if item["stage"] == stage),
                denominator_known=False,
            )
            for stage in sorted({item["stage"] for item in content_flow})
        }
    if health:
        result["source_health"] = health
    if findings:
        result["diagnostics"] = findings
    return result
