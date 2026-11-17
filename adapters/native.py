"""Import isolated Claude Code and Codex traces produced by official CLBench."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from evidence.archive import Archive
from evidence.schema import NativeRun, event

from .native_events import mapping, native_parts, native_session, records


SYSTEM_NAMES = {"claude_code": "claude", "codex": "codex"}


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _pointer(path: str) -> dict:
    return {"type": "json_pointer", "pointer": path}


def _with_pointer(ref: dict, pointer: str) -> dict:
    locator = dict(ref["locator"])
    if locator.get("type") == "json_pointer" and "line" not in locator:
        locator["pointer"] += pointer
    elif pointer:
        locator["line_pointer"] = pointer
    return {**ref, "locator": locator}


def _trace_paths(root: Path, sources: Iterable[str | Path] | None) -> Iterator[Path]:
    if sources is not None:
        paths = {root / Path(source) for source in sources}
    else:
        patterns = (
            "run_*.json*", "baseline.json*",
            "checkpoints/*/run-*.json*", "checkpoints/*/baseline-*.json*",
            "checkpoints/*/incomplete-*.json*", "checkpoints/*/live-*.json*",
            "traces/*/run_*.json*", "traces/*/baseline.json*",
            "*/traces/*/run_*.json*", "*/traces/*/baseline.json*",
            "failed_*.json*", "*/failed_*.json*",
        )
        paths = {path for pattern in patterns for path in root.glob(pattern)}
    for path in sorted(paths):
        if not path.is_file() or not path.name.endswith((".json", ".json.gz")):
            if sources is not None:
                raise ValueError(f"Expected an official CLBench JSON trace: {path}")
            continue
        if path.name.startswith("live-"):
            job_filename = path.name.removeprefix("live-")
            if any(path.with_name(prefix + job_filename).is_file() for prefix in ("", "incomplete-")):
                continue
        yield path


def _sidecar_files(root: Path, manifest: dict, system: str) -> Iterator[Path]:
    for name in records(manifest.get(f"{system}_conversation_jsonl_paths")):
        if not isinstance(name, str):
            continue
        path = PurePosixPath(name)
        if path.parts[:2] != (f"{system}_conversations", "final") or ".." in path.parts or path.suffix != ".jsonl":
            continue
        candidate = root / path
        if candidate.resolve().is_relative_to(root.resolve()) and candidate.is_file():
            yield candidate


def _embedded_lines(contents: str, archive: Archive, path: Path, pointer: str) -> Iterator[tuple[dict, dict, int]]:
    for line, text in enumerate(contents.splitlines(keepends=True), 1):
        if not text.strip():
            continue
        ref = archive.ref(path, {**_pointer(pointer), "line": line}, "native_events")
        try:
            row = json.loads(text)
            if not isinstance(row, dict):
                raise ValueError("Expected a JSON object")
        except ValueError:
            archive.diagnostics.append({
                "reason": "malformed_record" if text.endswith("\n") else "truncated_record", "raw_ref": ref,
            })
            row = None
        if row is not None:
            yield row, ref, line


def _native_streams(
    archive: Archive, path: Path, base: str, trace: dict, directory: Path, manifest: dict, system: str,
) -> Iterator[tuple[str, Iterator[tuple[dict, dict, int]]]]:
    artifacts = mapping(trace.get("system_artifacts"))
    cli_rows = artifacts.get("jsonl_events")
    if isinstance(cli_rows, list):
        pointer = base + "/system_artifacts/jsonl_events"
        yield "cli", (
            (mapping(row), archive.ref(path, _pointer(f"{pointer}/{index}"), "native_events"), index + 1)
            for index, row in enumerate(cli_rows)
        )
    elif (stdout := directory / "events.jsonl").is_file():
        yield "cli", archive.jsonl(stdout)

    conversations_key = f"{system}_conversation_jsonl_files"
    conversations = mapping(artifacts.get(conversations_key))
    if conversations:
        for filename, contents in sorted(conversations.items()):
            if isinstance(contents, str):
                pointer = base + f"/system_artifacts/{conversations_key}/{_escape(filename)}"
                yield f"session:{filename}", _embedded_lines(contents, archive, path, pointer)
    else:
        for session in _sidecar_files(directory, manifest, system):
            yield f"session:{session.relative_to(directory)}", archive.jsonl(session)


def _native_events(
    rows: Iterable[tuple[dict, dict, int]], harness: str, stream: str, source_id: str,
) -> Iterator[dict]:
    active_session = None
    seq = 0
    for row, raw_ref, native_seq in rows:
        row = mapping(row)
        session = native_session(row, harness)
        if session is not None:
            active_session = session
        scoped_session = session or active_session
        native_type = row.get("type")
        for kind, pointer, fields in native_parts(row, harness):
            seq += 1
            if native_type == "capture_error":
                kind, pointer = "capture_error", ""
            fields = {
                "native_type": native_type if isinstance(native_type, str) else None,
                "native_seq": native_seq,
                "native_event_id": row.get("uuid"),
                "native_parent_id": row.get("parentUuid"),
                "native_session_id": scoped_session,
                "thread_id": scoped_session if harness == "codex" else None,
                "mapping_status": "exact" if session else "scope_only" if scoped_session else "unresolved",
                "correlation_method": "native_session_id" if session else "native_stream_scope" if scoped_session else None,
                "source_stream": stream,
                "timestamp": row.get("timestamp"),
                **fields,
            }
            normalized = event(_with_pointer(raw_ref, pointer), kind, seq, **fields)
            normalized["source_id"] = source_id
            yield normalized


def _scores(archive: Archive, path: Path, base: str, trace: dict) -> Iterator[dict]:
    for index, outcome in enumerate(records(trace.get("instance_outcomes"))):
        if not isinstance(outcome, dict):
            continue
        reward = outcome.get("reward")
        valid = type(reward) in {int, float} and math.isfinite(reward)
        score = {
            "instance_id": outcome.get("instance_id"), "instance_index": outcome.get("instance_index"),
            "metric_name": "reward", "raw_score": reward if valid else None,
            "grading_status": "valid" if valid else "unscorable", "scorer_version": None,
            "details_ref": archive.ref(path, _pointer(f"{base}/instance_outcomes/{index}"), "native_score"),
        }
        if type(outcome.get("success")) is bool:
            score["native_success"] = outcome["success"]
        yield score
        value, metric = outcome.get("raw_metric_value"), outcome.get("raw_metric_name")
        if isinstance(metric, str) and metric != "reward" and type(value) in (int, float) and math.isfinite(value):
            yield {**score, "metric_name": metric, "raw_score": value, "grading_status": "valid",
                   "higher_is_better": outcome.get("raw_metric_higher_is_better")}


def _benchmark_events(
    archive: Archive, path: Path, base: str, trace: dict, origin: str, harness: str,
) -> tuple[list[dict], list[dict]]:
    events, versions, previous = [], [], {}
    for index, interaction in enumerate(records(trace.get("interactions"))):
        record = mapping(interaction)
        query = mapping(record.get("query"))
        response = mapping(record.get("response"))
        metadata = mapping(response.get("metadata"))
        identity = {
            "instance_id": query.get("instance_id"), "instance_index": query.get("instance_index"),
            "native_session_id": metadata.get("conversation_id"),
            "thread_id": metadata.get("conversation_id") if harness == "codex" else None,
            "timestamp": record.get("timestamp"),
        }
        prefix = f"{base}/interactions/{index}"
        fallback = isinstance(metadata.get("llm_error"), dict)
        for kind, field, layer, actor, present in (
            ("environment_query", "/query", "benchmark_query", "harness", bool(query)),
            ("environment_action", "/response/action", "benchmark_action", "harness" if fallback else "agent", "action" in response),
            ("environment_feedback", "/observation", "benchmark_observation", "harness", isinstance(record.get("observation"), dict)),
            ("harness_outcome", "/response/metadata/llm_error", "benchmark_error", "harness", fallback),
            ("state_boundary", "/response/metadata/continuity", "benchmark_continuity", "harness", isinstance(metadata.get("continuity"), dict)),
        ):
            if present:
                ref = archive.ref(path, _pointer(prefix + field), layer)
                extra = {"teacher_visible": False, "status": "failed"} if kind == "harness_outcome" else {}
                if kind == "environment_action":
                    extra["submission_status"] = "benchmark_fallback" if fallback else "benchmark_recorded"
                if kind == "state_boundary":
                    extra["boundary"] = metadata["continuity"].get("invocation_mode")
                events.append(event(ref, kind, len(events) + 1, actor_type=actor, **identity, **extra))
        memory = metadata.get("memory_files")
        if isinstance(memory, dict):
            scope = prefix + "/response/metadata/memory_files"
            ref = archive.ref(path, _pointer(scope), "native_memory_snapshot")
            snapshot = event(ref, "memory_snapshot", len(events) + 1, actor_type="harness", **identity)
            events.append(snapshot)
            for name, content in sorted(memory.items()):
                if not isinstance(content, str):
                    continue
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                parent = previous.get(name)
                if parent is not None and parent["content_hash"] == digest:
                    continue
                artifact_id = hashlib.sha256(f"{origin}:memory:{name}".encode()).hexdigest()
                version = {
                    "artifact_id": artifact_id, "artifact_role": "memory", "path": name,
                    "version_id": hashlib.sha256(f"{artifact_id}:{index}:{digest}".encode()).hexdigest(),
                    "parent_version_id": parent["version_id"] if parent else None,
                    "content_ref": archive.ref(path, _pointer(f"{scope}/{_escape(name)}"), "native_memory_snapshot"),
                    "content_hash": digest, "source_event_ids": [snapshot["event_id"]],
                    "observation_boundary": "post_interaction_snapshot", "existence_status": "present",
                }
                versions.append(version)
                previous[name] = version
            # A missing read may mean the file was unreadable. Break continuity until a new observation.
            previous = {name: version for name, version in previous.items() if isinstance(memory.get(name), str)}
    return events, versions


def _read_trace(
    archive: Archive, path: Path, base: str, trace: dict, directory: Path, manifest: dict, harness: str,
) -> NativeRun:
    system = SYSTEM_NAMES[harness]
    task = mapping(trace.get("task"))
    system_meta = mapping(trace.get("system"))
    params = mapping(system_meta.get("params"))
    execution = mapping(trace.get("execution"))
    artifacts = mapping(trace.get("system_artifacts"))
    relative = path.resolve().relative_to(archive.root).as_posix()
    origin = f"{harness}:{relative}#{base}"
    task_name = task.get("name")
    if task_name is None and path.name.startswith("failed_") and path.parent != archive.root:
        task_name = path.parent.name
    native_phase = trace.get("phase")
    if native_phase is None and path.name.startswith("failed_"):
        native_phase = "run"
    phase = "rollout" if native_phase in ("run", "rollout", "stateful") else native_phase
    identity = {
        "task": task_name, "phase": phase, "native_phase": native_phase,
        "job_id": origin.removesuffix("#"), "job_attempt_id": hashlib.sha256(origin.encode()).hexdigest(),
        "status": trace.get("status", "unknown"), "model_id": params.get("model"),
        "run_group_id": execution.get("run_group_id"), "job_index": execution.get("run_index"),
        "schedule": trace.get("schedule"), "harness_version": artifacts.get("version", manifest.get("version")),
        "created_at": execution.get("start_time"), "ended_at": execution.get("end_time"),
        "native_created_at": execution.get("start_time"), "native_ended_at": execution.get("end_time"),
        "trace_ref": archive.ref(path, _pointer(base) if base else None, "benchmark_trace"),
        "capture_capabilities": {
            "source": "official_clbench_artifacts", "actual_context": "unavailable",
            "physical_attempts": "unavailable", "memory": "post_interaction_snapshots",
            "context_changes": "native_boundaries_only",
        },
        "missing_reasons": {
            "actual_context_ref": "Official CLI recordings do not provide the final model input.",
            "physical_attempts": "Official CLI turn events can encompass multiple unrecorded provider calls.",
        },
    }
    diagnostic_start = len(archive.diagnostics)
    events, versions = _benchmark_events(archive, path, base, trace, origin, harness)
    if isinstance(trace.get("error"), dict):
        ref = archive.ref(path, _pointer(base + "/error"), "benchmark_error")
        events.append(event(ref, "harness_outcome", len(events) + 1, actor_type="harness", status="failed", teacher_visible=False))
    for stream, rows in _native_streams(archive, path, base, trace, directory, manifest, system):
        events.extend(_native_events(rows, harness, stream, f"{origin}/{stream}"))
    capabilities = identity["capture_capabilities"]
    capabilities["cli_events"] = "observed_subset" if any(row.get("source_stream") == "cli" for row in events) else "unknown"
    capabilities["sessions"] = "observed_subset" if any(row.get("source_stream", "").startswith("session:") for row in events) else "unknown"
    caps_ref = directory / "manifest.json"
    sources = [identity["trace_ref"]]
    if caps_ref.is_file():
        sources.append(archive.ref(caps_ref, layer="official_artifact_manifest"))
    return NativeRun(identity=identity, events=events, requests=[], versions=versions,
                     scores=list(_scores(archive, path, base, trace)), sources=sources,
                     diagnostics=archive.diagnostics[diagnostic_start:])


def _baseline_dir(directory: Path, trace: dict) -> Path:
    index = mapping(trace.get("execution")).get("run_index")
    if type(index) is not int:
        index = next((outcome["instance_index"] for outcome in records(trace.get("instance_outcomes"))
                      if isinstance(outcome, dict) and type(outcome.get("instance_index")) is int), None)
    return directory / "instances" / f"instance_{index:04d}" if index is not None else directory


def read_runs(archive: Archive, *, harness: str, sources: Iterable[str | Path] | None = None) -> Iterator[NativeRun]:
    """Read standalone traces or CLBench traces with sibling artifact exports."""
    system = SYSTEM_NAMES[harness]
    for path in _trace_paths(archive.root, sources):
        trace = archive.json(path)
        if not isinstance(trace, dict):
            raise ValueError(f"Expected a CLBench trace object: {path}")
        directory = path.parent / "artifacts" / path.name.removesuffix(".gz").removesuffix(".json")
        manifest_path = directory / "manifest.json"
        manifest = mapping(archive.json(manifest_path)) if manifest_path.is_file() else {}
        system_name = mapping(trace.get("system")).get("name")
        artifact_type = mapping(trace.get("system_artifacts")).get("artifact_type") or manifest.get("artifact_type")
        compatible_system = system_name in (system, harness) if system_name is not None else artifact_type == system
        if not compatible_system:
            if sources is not None:
                raise ValueError(f"Trace does not belong to {harness}: {path}")
            continue
        instances = trace.get("instance_traces")
        if trace.get("phase") == "baseline" and isinstance(instances, list):
            for index, instance in enumerate(instances):
                if not isinstance(instance, dict):
                    continue
                sidecar = _baseline_dir(directory, instance)
                instance_manifest = sidecar / "manifest.json"
                submanifest = mapping(archive.json(instance_manifest)) if instance_manifest.is_file() else {}
                if not submanifest:
                    relative = sidecar.relative_to(directory).as_posix()
                    entry = next((item for item in records(manifest.get("instances"))
                                  if isinstance(item, dict) and item.get("path") == relative), {})
                    submanifest = mapping(entry.get(f"{system}_manifest"))
                run = _read_trace(archive, path, f"/instance_traces/{index}", instance, sidecar, submanifest, harness)
                if manifest_path.is_file():
                    run.sources.append(archive.ref(manifest_path, layer="official_artifact_manifest"))
                yield run
            for index, blocked in enumerate(records(trace.get("blocked_instances"))):
                if not isinstance(blocked, dict):
                    continue
                location = f"/blocked_instances/{index}"
                metadata = {key: trace[key] for key in ("task", "system", "schedule") if key in trace}
                metadata.update(phase="baseline", status="blocked")
                run = _read_trace(archive, path, location, metadata, directory / "blocked", {}, harness)
                run.events.append(event(
                    archive.ref(path, _pointer(location), "benchmark_error"), "harness_outcome", 1,
                    actor_type="harness", status="blocked", teacher_visible=False,
                    instance_id=blocked.get("instance_id"), instance_index=blocked.get("instance_index"),
                ))
                yield run
            continue
        if trace.get("phase") == "baseline" and len(records(trace.get("instance_outcomes"))) > 1:
            raise ValueError(f"Aggregated baseline has no isolated instance traces: {path}")
        yield _read_trace(archive, path, "", trace, directory, manifest, harness)
