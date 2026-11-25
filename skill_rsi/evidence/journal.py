"""Crash-safe collection of an authorized local event stream."""

import json
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING

from ..contracts.events import normalize_event
from .archive import Archive
from .build import publish
from .io import digest, file_lock, write_bytes
from .schema import NativeRun, encode, identifier

if TYPE_CHECKING:
    from ..sources.base import Origin

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ordinal INTEGER PRIMARY KEY, project TEXT NOT NULL, run TEXT NOT NULL,
    event TEXT NOT NULL, payload TEXT NOT NULL, semantic_digest TEXT NOT NULL,
    UNIQUE (project, run, event)
);
CREATE TABLE IF NOT EXISTS receipts (
    project TEXT NOT NULL, run TEXT NOT NULL, event TEXT NOT NULL,
    receipt TEXT NOT NULL, variant TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (project, run, event, receipt, variant)
);
CREATE TABLE IF NOT EXISTS streams (
    id TEXT PRIMARY KEY, cursor TEXT NOT NULL, initial_cursor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gaps (
    stream TEXT NOT NULL, position TEXT NOT NULL, reason TEXT NOT NULL, ref TEXT NOT NULL,
    PRIMARY KEY (stream, position, reason)
);
"""
_SOURCE_FIELDS = {"raw_ref", "source_ref", "cursor", "schema_version", "project_id", "run_id", "source_id"}
_RAW_PAYLOAD_FIELDS = {"native_record", "visible_state", "messages", "content", "checkpoint_ref", "decision"}
_DECISION_METADATA = {"decision_id", "operation_key", "stage", "status", "mode", "component_id", "version",
                      "binding_revision", "artifact_digest", "trigger_ref", "memory_before", "memory_after"}
_IMPORT_FIELDS = {"source_stream", "mapping_version", "mapping_status", "run_id_derived", "native_timestamp"}
_RECEIPT_TYPES = {
    "component_decision", "component_receipt", "operation_receipt", "memory_operation", "tool_result",
    "environment_action",
}


def _json(value):
    return encode(value).decode("utf-8")


def _required(event, name):
    value = event.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Event requires a non-empty {name}")
    return value


def _checkpoint(connection, stream, cursor):
    if stream is None:
        return
    connection.execute(
        "INSERT INTO streams VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor",
        (stream, _json(cursor), _json(cursor)),
    )


def _score_evidence(row):
    name, scorer, status = (row.get(key) for key in ("metric_name", "scorer_version", "grading_status"))
    instance, index = row.get("instance_id"), row.get("instance_index")
    if not all(isinstance(item, str) and item for item in (name, scorer)) or (
        type(instance) not in {str, int} or instance == "" or type(index) is not int or index < 0
    ):
        raise ValueError("Official score requires an instance, metric, and scorer version")
    if status not in {"valid", "pending", "unscorable"}:
        raise ValueError("Unknown official grading status")
    raw = row.get("raw_score")
    if status == "valid":
        if type(raw) not in {int, float} or not isfinite(raw):
            raise ValueError("Official scores require a finite value")
    elif raw is not None:
        raise ValueError("Pending or unscorable official scores cannot have a numeric value")
    reasons = row.get("missing_reasons") or []
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        raise ValueError("Score missing_reasons must be a list of strings")
    success = row.get("native_success", row.get("success"))
    higher = row.get("higher_is_better")
    direction = row.get("direction")
    if direction is None and type(higher) is bool:
        direction = "higher" if higher else "lower"
    return {
        **{key: row.get(key) for key in ("metric_name", "unit", "scorer_version",
           "raw_score", "reward", "grading_status", "instance_id", "instance_index")},
        "direction": direction, "higher_is_better": higher if type(higher) is bool else None,
        "native_success": success if type(success) is bool else None,
        "missing_reasons": reasons, "source_event_ids": [row["event_id"]], "raw_ref": row["raw_ref"],
    }


class EventJournal:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.root / "journal.sqlite3"
        with self._connection() as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def cursor(self, stream):
        with self._connection() as connection:
            row = connection.execute("SELECT cursor FROM streams WHERE id=?", (stream,)).fetchone()
        return json.loads(row[0]) if row else None

    def _source_ref(self, event, origin):
        content = origin.content if origin else encode(event) + b"\n"
        sha = digest(content)
        suffix = ".jsonl" if origin and origin.format == "jsonl" else ".json"
        relative = Path("native", "journal", sha[:2], f"{sha}{suffix}")
        path = self.root / relative
        if not path.exists():
            try:
                write_bytes(path, content, replace=False)
            except FileExistsError:
                pass
        locator = origin.locator if origin else {"type": "file"}
        if suffix == ".jsonl":
            locator = {"type": "line", "line": 1}
        return {
            "path": relative.as_posix(), "bytes": len(content), "sha256": sha,
            "source_root": "batch", "locator": locator, "payload_layer": "native_events",
        }

    def append(self, event, *, origin: "Origin | None" = None, cursor=None, stream=None, sequence_is_identity=False):
        project = _required(event, "project_id")
        run = _required(event, "run_id")
        source = _required(event, "source_id")
        origin_position = origin.position() if origin else None
        recorded = {key: value for key, value in event.items() if key not in _SOURCE_FIELDS | _RAW_PAYLOAD_FIELDS}
        decision = event.get("decision")
        if event.get("type") in {"component_decision", "component_receipt"} and isinstance(decision, dict):
            recorded = {**{key: decision[key] for key in _DECISION_METADATA if key in decision}, **recorded}
        ref = self._source_ref(event, origin)
        normalized = normalize_event(
            recorded, project_id=project, run_id=run, source_id=source, source_ref=ref,
            origin=origin_position, sequence_is_identity=sequence_is_identity,
        )
        event_id = normalized["event_id"]
        if normalized["type"] == "official_score":
            _score_evidence(normalized)
        comparable = {
            key: value for key, value in normalized.items()
            if key not in (*_SOURCE_FIELDS, *_IMPORT_FIELDS, "visibility")
        }
        variant = digest(encode(comparable))
        sighting = {"source_id": source, "source_ref": ref, "position": origin_position,
                    "source_stream": event.get("source_stream")}
        with self._connection() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO events (project,run,event,payload,semantic_digest) VALUES (?,?,?,?,?)",
                (project, run, event_id, _json(normalized), variant),
            ).rowcount
            connection.execute(
                "INSERT OR IGNORE INTO receipts VALUES (?,?,?,?,?,?)",
                (project, run, event_id, _json(sighting), variant, _json(sighting)),
            )
            _checkpoint(connection, stream, cursor)
        return {"event_id": event_id, "duplicate": not inserted}

    def skip(self, origin, cursor, *, stream):
        reason = cursor.get("skip_reason", "unmapped_record")
        with self._connection() as connection:
            if reason != "blank":
                connection.execute(
                    "INSERT OR IGNORE INTO gaps VALUES (?,?,?,?)",
                    (stream, _json(origin.position()), reason, _json(self._source_ref({}, origin))),
                )
            _checkpoint(connection, stream, cursor)

    def _runs(self):
        by_run = defaultdict(list)
        with self._connection() as connection:
            connection.execute("BEGIN")
            for project, run, event, content, variant in connection.execute(
                "SELECT project, run, event, payload, semantic_digest FROM events ORDER BY ordinal"
            ):
                sightings = connection.execute(
                    "SELECT payload, variant FROM receipts WHERE project=? AND run=? AND event=? ORDER BY rowid",
                    (project, run, event),
                )
                row = json.loads(content)
                row["source_receipts"] = [{**json.loads(ref), "variant": other}
                                          for ref, other in sightings]
                row["source_conflict"] = any(receipt["variant"] != variant for receipt in row["source_receipts"])
                by_run[project, run].append(row)
            health = {
                stream: {
                    "source_id": stream, "capture_profile": "journal",
                    "start_cursor": json.loads(initial), "end_cursor": json.loads(cursor),
                    "sampling_rule": "all_consumed_records", "gap_ranges": [],
                    "dropped_count": None, "close_status": json.loads(cursor).get("close_status", "unknown"),
                }
                for stream, cursor, initial in connection.execute(
                    "SELECT id, cursor, initial_cursor FROM streams ORDER BY id"
                )
            }
            for stream, position, reason, ref in connection.execute(
                "SELECT stream, position, reason, ref FROM gaps ORDER BY rowid"
            ):
                health[stream]["gap_ranges"].append(
                    {"cursor": json.loads(position), "reason": reason, "source_ref": json.loads(ref)}
                )
        for (project, native_run), rows in by_run.items():
            source_ids = {row["source_id"] for row in rows}
            # Source progress keys are scoped by stream, including project and import batch.
            scopes = {receipt.get("source_stream") for row in rows for receipt in row["source_receipts"]}
            streams = [entry for key, entry in health.items() if key in scopes]
            for index, row in enumerate(rows, 1):
                if row["seq"] is None:
                    row["seq"] = index
                row["source_event_schema"] = row["schema_version"]
            terminal = next((row for row in reversed(rows) if row["type"] in {"run_finished", "run_failed"}), None)
            assignment = rows[0].get("assignment")
            assignment = assignment if isinstance(assignment, dict) else {}
            run = NativeRun(
                {
                    "run_id": identifier("journal", project, native_run),
                    "native_run_id": native_run, "project_id": project,
                    "lineage_id": rows[0].get("lineage_id"),
                    "task_name": rows[0].get("task_name", rows[0].get("task_id")),
                    "schedule": rows[0].get("schedule", assignment.get("schedule")),
                    "seed": rows[0].get("seed", assignment.get("seed")),
                    "model_id": rows[0].get("model_id", assignment.get("model")),
                    "harness_version": rows[0].get("harness_version", assignment.get("host_version")),
                    "environment_version": rows[0].get("environment_version"),
                    "experiment_protocol_id": rows[0].get("experiment_protocol_id"),
                    "benchmark_evaluation_only": any(row.get("evaluation_only") is True for row in rows),
                    "created_at": rows[0].get("native_timestamp"),
                    "ended_at": terminal.get("native_timestamp") if terminal else None,
                    "status": {"run_finished": "completed", "run_failed": "failed"}.get(
                        terminal["type"] if terminal else None, "unknown"
                    ),
                    "capture_capabilities": {"journal_sources": sorted(source_ids)},
                }, events=rows,
            )
            if streams:
                run.observations["source_health"] = streams
            for entry in streams:
                run.diagnostics.extend(entry["gap_ranges"])
            operations = defaultdict(list)
            for row in rows:
                if row.get("operation_key") and row["type"] in _RECEIPT_TYPES:
                    operations[row["operation_key"]].append({
                        "source_event_id": row["event_id"], "stage": row.get("stage") or row["type"],
                        "status": row.get("status"), "evidence_ref": row["raw_ref"],
                    })
                if row["type"] == "memory_version" and row.get("object_id") and row.get("version"):
                    run.versions.append({
                        "version_id": identifier(project, row["object_id"], row["version"]),
                        "artifact_id": row["object_id"], "version": row["version"],
                        "parent_version": row.get("parent_version"), "validity": row.get("validity"),
                        "source_event_ids": [row["event_id"]], "content_ref": row["raw_ref"],
                    })
                elif row["type"] == "official_score":
                    run.scores.append(_score_evidence(row))
                if row["source_conflict"]:
                    run.diagnostics.append({"reason": "native_event_content_conflict", "event_id": row["event_id"]})
            if operations:
                run.observations["operations"] = [
                    {"operation_key": key, "receipts": items} for key, items in operations.items()
                ]
            yield run

    def publish(self, output, *, seal=True):
        with file_lock(self.root / ".publish.lock"):
            with self._connection() as connection:
                stream_count = connection.execute("SELECT count(*) FROM streams").fetchone()[0]
                gap_count = connection.execute("SELECT count(*) FROM gaps").fetchone()[0]
            return publish(
                Archive(self.root), "local", self._runs(), output=output, seal=seal,
                coverage={"checkpointed_streams": stream_count, "recorded_source_gaps": gap_count},
            )
