"""Canonical event identity, visibility, and extension record versions."""

from collections import Counter
from collections.abc import Mapping
from typing import Any

from ..evidence.schema import identifier, utc

EVENT_SCHEMA = "SkillRSI-Event-v1"
DIAGNOSTICS_SCHEMA = "SkillRSI-Observation-v1"
OBSERVATIONS = frozenset({"present", "absent", "unknown", "not_applicable"})


def normalize_event(
    data: Mapping[str, Any],
    *,
    project_id: str,
    run_id: str,
    source_id: str,
    source_ref: Mapping[str, Any],
    origin: Mapping[str, Any] | None = None,
    sequence_is_identity: bool = False,
) -> dict:
    """Use native identity when qualified, or an immutable source position."""
    fields = dict(data)
    raw_seq = fields.get("seq")
    if raw_seq is not None and (type(raw_seq) is not int or raw_seq < 0):
        raise ValueError("Event seq must be a non-negative integer")
    kind = fields.get("type")
    if not isinstance(kind, str) or not kind:
        raise ValueError("Event type is required")
    native_session = fields.get("native_session_id") or fields.get("thread_id")
    native_id = fields.get("native_event_id")
    immutable = dict(origin or source_ref)
    if native_id is not None:
        key = ("native_id", native_session, native_id)
    elif fields.get("event_id"):
        key = ("canonical_id", fields["event_id"])
    elif sequence_is_identity and native_session is not None and raw_seq is not None:
        key = ("native_sequence", native_session, raw_seq)
    else:
        key = ("source_position", immutable)
    stamp = fields.get("native_timestamp", fields.get("timestamp"))
    visible = fields.get("visibility") or {"native_record": "observed", "model_input": "unknown"}
    return {
        **fields,
        "schema_version": EVENT_SCHEMA,
        "project_id": project_id,
        "run_id": run_id,
        "source_id": source_id,
        "event_id": identifier(project_id, source_id, run_id, key),
        "cursor": origin or None,
        "source_event_id": fields.get("event_id"),
        "seq": raw_seq,
        "native_timestamp": stamp,
        "timestamp": utc(stamp),
        "visibility": visible,
        "raw_ref": dict(source_ref),
        "source_ref": dict(source_ref),
        "identity_basis": key[0],
    }


def observation_counts(rows, *, denominator_known: bool) -> dict:
    """Keep unknown evidence and inapplicable opportunities distinct."""
    counts = Counter()
    for row in rows:
        status = row["observation"]
        if status not in OBSERVATIONS:
            raise ValueError(f"Invalid observation: {status}")
        counts[status] += 1
    measured = counts["present"] + counts["absent"]
    eligible = measured + counts["unknown"]
    return {
        **{status: counts[status] for status in sorted(OBSERVATIONS)},
        "denominator_known": denominator_known,
        "measured": measured,
        "eligible_recorded": eligible,
        "known_coverage": measured / eligible if eligible and denominator_known else None,
        "present_rate_known": counts["present"] / measured if measured else None,
        "overall_rate_bounds": (
            [counts["present"] / eligible, (counts["present"] + counts["unknown"]) / eligible]
            if eligible and denominator_known else None
        ),
    }
