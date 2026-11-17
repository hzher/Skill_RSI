"""Build immutable per-attempt evidence views from allowlisted native sources."""

import fcntl
import os
import re
import shutil
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from . import dsh
from .archive import Archive, write
from .schema import GENERATOR, SCHEMA, identifier, references, utc
from .trajectory import build_trajectory, context_episodes

FOLDER = Path("derived") / "skill-rsi-v1"
HARNESSES = ("dsh", "claude_code", "codex")
FILES = (
    "manifest.json", "events.jsonl", "requests/index.jsonl", "trajectory.ctx.json",
    "context_episodes.jsonl", "scores.jsonl", "quality.json",
)


def _identity(run, harness):
    data = run.identity
    attempt_id = data.get("job_attempt_id", data.get("attempt_id"))
    return data.get("run_id") or identifier(
        harness, data.get("batch_id"), data.get("task", data.get("task_name")),
        data.get("job_id"), attempt_id or sorted(ref["path"] for ref in run.sources),
    )


def _inventory(value):
    files = {}
    for ref in references(value):
        key = ref["source_root"], ref["path"]
        layers = files.setdefault(key, {**ref, "locator": {"type": "file"}, "payload_layers": set()})
        if layers["sha256"] != ref["sha256"]:
            raise ValueError(f"Conflicting hashes for {ref['path']}")
        layers["payload_layers"].add(ref["payload_layer"])
    return [
        {**item, "payload_layers": sorted(item["payload_layers"])}
        for _, item in sorted(files.items())
    ]


def _seal(archive, staged, harness, refs):
    locations = {}
    sealed_archive = Archive(staged, "run")
    for ref in refs:
        if ref["source_root"] != "batch":
            continue
        key = ref["path"], ref["sha256"]
        if key not in locations:
            origin = archive.path(ref["path"])
            archive.assert_unchanged(origin)
            is_memory = ref["payload_layer"] == "memory_snapshot"
            relative = (
                Path("snapshots", "objects", ref["sha256"]) if is_memory else
                Path("raw", harness, "native", ref["sha256"], origin.name)
            )
            destination = staged / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copyfile(origin, destination)
            if sealed_archive.ref(destination)["sha256"] != ref["sha256"]:
                raise ValueError(f"Sealed copy hash differs: {origin}")
            archive.assert_unchanged(origin)
            locations[key] = relative.as_posix()
        ref["path"] = locations[key]
        ref["source_root"] = "run"


def _quality(run, episodes, sealed):
    requests = run.requests
    events = Counter(row["type"] for row in run.events)
    labels = Counter(row.get("label_status", "not_applicable") for row in run.events)
    health = run.identity.get("runtime_health") or {}
    flags = []
    open_calls = []
    for contents in health.values():
        if contents.get("used") is True and contents.get("durable_prefix_only") is not None:
            flags.append(not contents["durable_prefix_only"])
        elif "disposed" in contents:
            flags.append(contents["disposed"])
        elif contents.get("recording_closed", contents.get("closed")) is not None:
            flags.append(contents.get("recording_closed", contents.get("closed")))
        if isinstance(contents.get("open_calls"), list):
            open_calls.extend(contents["open_calls"])
    unresolved = sum(row.get("mapping_status") in {"ambiguous", "unresolved"} for row in requests)
    unresolved_actions = sum(row.get("mapping_status") == "unresolved" for row in run.events
                             if row["type"] == "environment_action")
    submissions = Counter(row.get("submission_status", "unknown") for row in run.events
                          if row["type"] == "environment_action")
    return {
        "sources_sealed": sealed, "runtime_health": health,
        "recording_closed": False if False in flags else True if flags and all(flag is True for flag in flags) else None,
        "event_counts": dict(events), "request_rows": len(requests), "score_rows": len(run.scores),
        "requests_with_available_payload": sum(bool(row.get("request_ref")) for row in requests),
        "requests_with_verified_actual_context": sum(bool(row.get("actual_context_ref")) for row in requests),
        "requests_with_observed_transport_attempt": sum(bool(row.get("attempt_id")) for row in requests),
        "requests_with_observed_response": sum(bool(row.get("response_ref")) for row in requests),
        "observed_request_scope": "indexed_native_calls" if requests else "unknown",
        "recorded_open_native_calls": len(open_calls) if any("open_calls" in row for row in health.values()) else None,
        "open_attempts": None,
        "unresolved_links": {"call_to_instance": unresolved if requests else None,
                             "official_action_to_native_submission": unresolved_actions if events["environment_action"] else None},
        "official_submission_links": dict(submissions),
        "label_scope": "all_indexed_events", "label_denominator": len(run.events),
        "label_counts": dict(labels), "context_episode_count": len(episodes),
        "episodes_scope": "observed_candidates" if episodes else "none_identified_in_available_sources",
        "snapshots_count": len(run.versions),
        "missing_refs": sum(row.get("reason", "").startswith("missing_") for row in run.diagnostics),
        "truncated_records": sum(row.get("reason") == "truncated_record" for row in run.diagnostics),
        "dropped_records": None,
        "diagnostics": run.diagnostics,
        "part1_acceptance": "unverified", "training_eligibility": "not_qualified",
        "training_eligibility_reasons": (["benchmark_evaluation_only"] if run.identity.get("clbench_commit")
                                        or run.identity.get("trace_ref") else ["training_review_pending"]),
        "sft_samples": 0,
    }


def _manifest(run, harness, run_id, trajectory, episodes, sealed, source_fingerprint):
    identity = run.identity
    req = run.requests
    task = identity.get("task_name", identity.get("task"))
    sources = [
        {"source_id": row["path"], "kind": row["payload_layer"], "version": None,
         "location": row, "payload_layer": row["payload_layer"], "missing_reasons": [],
         "sealing": "snapshot_copy" if sealed else "hash_reference"}
        for row in _inventory(run.sources + run.events + req + run.versions + run.scores)
    ]
    candidates = [row for row in episodes if row["operation_ids"] or row["application_status"] == "applied"]
    instances = {str(row["instance_id"]): row.get("instance_index") for row in (*run.scores, *run.events)
                 if row.get("instance_id") is not None}
    return {
        **identity, "run_id": run_id, "harness_name": harness, "task_name": task,
        "created_at": utc(identity.get("created_at")), "ended_at": utc(identity.get("ended_at")),
        "status": {"completed": "finished", "failed": "error", "incomplete": "stopped",
                   "cancelled": "stopped", "interrupted": "stopped"}.get(identity.get("status"), identity.get("status", "unknown")),
        "native_status": identity.get("status"), "source_fingerprint": source_fingerprint,
        "sources": sources, "clbench_commit": identity.get("clbench_commit"),
        "instances_observed": [{"instance_id": key, "instance_index": value} for key, value in instances.items()],
        "session_ids": sorted({str(row["native_session_id"]) for row in trajectory["trajectories"]
                               if row.get("native_session_id")}),
        "thread_ids": sorted({str(row["thread_id"]) for row in trajectory["trajectories"] if row.get("thread_id")}),
        "capture_plan": {"capture_mode": "native_plugin" if harness == "dsh" else "native",
                         "reason": "native_checkpoint_and_observation_sources"},
        "capture_capabilities": {
            **(identity.get("capture_capabilities") or {}),
            "native_events": "observed" if run.events else "unknown",
            "endpoint_requests": "observed_subset" if any(row.get("actual_context_ref") for row in req) else "unknown",
            "response_stream": "observed_subset" if any((row.get("response_ref") or {}).get("payload_layer")
                                                       == "native_output_chunks" for row in req) else "unknown",
            "context_chain": "available" if req and all(row.get("actual_context_ref") for row in req) else "unknown",
            "tool_raw": "observed_subset" if any(row.get("rendered_result_ref") for row in run.events) else "unknown",
            "memory_versions": "observed_subset" if run.versions else "unknown",
            "child_sessions": "unknown",
        },
        "initial_state_refs": identity.get("initial_state_refs"),
        "reset_policy": identity.get("reset_policy"),
        "memory_scope": identity.get("memory_scope"),
        "capture_health": identity.get("runtime_health"),
        "capture_overhead": identity.get("capture_overhead"),
        "parent_run_id": identity.get("parent_run_id"), "resume_from": identity.get("resume_from"),
        "skill_evidence_index": [{
            "evidence_id": row["context_episode_id"], "source_event_ids": row["source_event_ids"],
            "trajectory_path": "trajectory.ctx.json", "context_episode_ids": [row["context_episode_id"]],
            "artifact_version_refs": row["artifact_version_refs"],
        } for row in candidates],
    }


def _publish(archive, output, harness, run, sealed):
    source_files = _inventory([run.sources, run.events, run.requests, run.versions, run.scores, run.identity, run.diagnostics])
    fingerprint = identifier(GENERATOR, harness, sealed, run.identity, source_files)
    base_id = _identity(run, harness)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", base_id):
        raise ValueError(f"Invalid native run ID: {base_id!r}")
    run_dir = output / "runs" / base_id
    if run_dir.exists():
        saved = Archive(run_dir, "run").json("manifest.json")
        if saved.get("source_fingerprint") != fingerprint:
            run_dir = run_dir.with_name(f"{base_id[:100]}-{fingerprint}")
    run_id = run_dir.name
    stamp = {"schema_version": SCHEMA, "generator_version": GENERATOR, "run_id": run_id}
    if not run_dir.exists():
        trajectory = build_trajectory(archive, run, run_id)
        episodes = context_episodes(run, run_id)
        quality = _quality(run, episodes, sealed)
        manifest = _manifest(run, harness, run_id, trajectory, episodes, sealed, fingerprint)
        manifest["source_run_id"] = base_id
        with TemporaryDirectory(prefix=".run-", dir=run_dir.parent) as temp:
            staged = Path(temp) / run_id
            staged.mkdir()
            payloads = (manifest, run.events, run.requests, trajectory, episodes, run.scores, quality)
            version_stamp = {**stamp, "versions": run.versions}
            raw_refs = list(references([payloads, version_stamp, run.sources]))
            if sealed:
                _seal(archive, staged, harness, raw_refs)
            for name, item in zip(FILES, payloads, strict=True):
                lines = name.endswith(".jsonl")
                write(staged / name, ({**row, **stamp} for row in item) if lines else {**item, **stamp}, lines=lines)
            published = Archive(staged, "run")
            files = _inventory(raw_refs + [published.ref(name, layer="derived_record") for name in FILES])
            write(staged / "artifacts.json", {**version_stamp, "files": files})
            os.rename(staged, run_dir)
    published = Archive(run_dir, "run")
    manifest = published.json("manifest.json")
    if manifest.get("source_fingerprint") != fingerprint:
        raise ValueError(f"Conflicting evidence already exists at {run_dir}")
    return {
        "run_id": run_id, "source_run_id": base_id, "task": manifest.get("task_name"),
        "job_id": manifest.get("job_id"), "job_attempt_id": manifest.get("job_attempt_id"),
        "previous_attempt_id": manifest.get("previous_attempt_id"),
        "phase": manifest.get("phase"), "status": manifest["status"],
        "runtime_ids": manifest.get("runtime_ids", []), "artifact_manifest": published.ref("artifacts.json", layer="derived_manifest"),
    }


def build(batch_dir, harness="dsh", *, output=None, sources=None, seal=False):
    if harness not in HARNESSES:
        raise ValueError(f"Unsupported harness: {harness}")
    archive = Archive(batch_dir)
    output = Path(output).resolve() if output else archive.root / FOLDER
    if output.is_relative_to(archive.root) and not output.is_relative_to(archive.root / "derived"):
        raise ValueError("Evidence output inside the batch must be under derived/")
    output.mkdir(parents=True, exist_ok=True)
    (output / "runs").mkdir(exist_ok=True)
    with (output / ".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = output / "batch_index.json"
        note = Archive(output).json(prior) if prior.exists() else {}
        if note and note.get("batch_id") != archive.root.name:
            raise ValueError("Use a distinct output directory for each source batch")
        coverage = {}
        if harness == "dsh":
            native = dsh.read_runs(archive, sources=sources, coverage=coverage)
        else:
            from adapters.native import read_runs

            native = read_runs(archive, harness=harness, sources=sources)
        runs = [_publish(archive, output, harness, run, seal) for run in native]
        archive.assert_unchanged()
        previous_import = note.get("harness_imports", {}).get(harness, {})
        if sources:
            by_source_run = {row["source_run_id"]: row for row in previous_import.get("runs", [])}
            by_source_run.update({row["source_run_id"]: row for row in runs})
            runs = list(by_source_run.values())
        imports = {
            **note.get("harness_imports", {}),
            harness: {"runs": runs, "counts_by_status": dict(Counter(row["status"] for row in runs)),
                      "coverage": coverage, "sources_sealed": seal},
        }
        index = {
            "schema_version": SCHEMA, "generator_version": GENERATOR, "batch_id": archive.root.name,
            "batch_local": output == archive.root / FOLDER,
            "harness_imports": imports, "runs": [row for entry in imports.values() for row in entry["runs"]],
        }
        write(prior, index)
    return {"archive": str(output), "harness": harness, "run_count": len(runs),
            "counts_by_status": index["harness_imports"][harness]["counts_by_status"], "sealed": seal}
