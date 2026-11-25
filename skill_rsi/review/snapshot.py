"""Freeze purpose-scoped, source-backed windows for downstream consumers."""

import json
import re
from collections import deque
from collections.abc import Mapping
from pathlib import Path

from ..evidence.io import checked_bytes, digest, indexed_run, inside, read_json, write_bytes
from ..evidence.schema import encode, identifier
from .projection import INDEX_FILES, PRIVATE_EXCLUSIONS, REF_FIELDS, allowed_payload, documents, metadata

SCHEMA = "SkillRSI-ReviewSnapshot-v1"
_USES = {"discovery": "discovery", "evaluation": "evaluation", "training": "weight_training"}
_SPLITS = {"discovery": {"development"}, "evaluation": {"holdout"}, "training": {"train", "dev"}}
_BOUNDARIES = {
    "session_resume", "resume", "compaction_installed", "context_change", "constraint_use",
    "memory_opportunity", "model_request", "tool_call", "task_query", "decision_state", "training_state",
}


def _split_plan(spec):
    groups = spec.get("run_groups")
    if groups is None:
        groups = {"development": spec.get("development_groups", {}), "holdout": spec.get("holdout_groups", {})}
    if not isinstance(groups, Mapping) or groups.keys() - {"development", "holdout", "train", "dev"}:
        raise ValueError("run_groups must map known splits to frozen groups")
    planned = {}
    all_group_ids = set()
    memberships = {}
    for split, selected in groups.items():
        if isinstance(selected, list) and split == "holdout":
            if any(not isinstance(group, str) or not group for group in selected) or len(selected) != len(set(selected)):
                raise ValueError("Invalid holdout group names")
            selected = dict.fromkeys(selected)
        if not isinstance(selected, Mapping):
            raise ValueError(f"{split} requires a group-to-run-ID mapping")
        planned[split] = {}
        for group, run_ids in selected.items():
            if not isinstance(group, str) or not group or group in all_group_ids:
                raise ValueError(f"Split groups must be unique: {group!r}")
            all_group_ids.add(group)
            if run_ids is None and split == "holdout":
                planned[split][group] = None
                continue
            if not isinstance(run_ids, list) or not run_ids:
                raise ValueError(f"Group {group} must contain explicit run IDs")
            planned[split][group] = list(run_ids)
            for run in run_ids:
                if not isinstance(run, str) or not run or run in memberships:
                    raise ValueError(f"Duplicate or invalid run in split plan: {run!r}")
                memberships[run] = (split, group)
    return planned, memberships


def _initial_environment(manifest):
    task, schedule, seed = (manifest.get(key) for key in ("task_name", "schedule", "seed"))
    if task and schedule is not None and seed is not None:
        return identifier(task, schedule, seed)
    return None


def _lineage_keys(manifest, run):
    project = manifest.get("project_id")
    lineage = manifest.get("lineage_id")
    source_run = manifest.get("source_run_id")
    parent = manifest.get("parent_run_id")
    yield "run", run
    if manifest.get("source_fingerprint"):
        yield "source_fingerprint", manifest["source_fingerprint"]
    if manifest.get("source_content_sha256"):
        yield "source_content", manifest["source_content_sha256"]
    if manifest.get("job_attempt_id"):
        yield "job_attempt", manifest["job_attempt_id"]
    for identity in (source_run, parent):
        if isinstance(identity, str) and identity:
            yield "run", identity
    if project:
        yield "project", project
    if lineage:
        yield "lineage", lineage
    if environment := _initial_environment(manifest):
        yield "initial_environment", environment


def _freeze_memberships(archive, batch_index, memberships):
    runs = {}
    splits = {}
    for run, (split, group) in memberships.items():
        run_dir, refs, artifacts = indexed_run(archive, run, batch_index)
        manifest = json.loads(checked_bytes(run_dir, "manifest.json", refs["manifest.json"]))
        if manifest.get("run_id") != run:
            raise ValueError(f"Manifest identity mismatch for {run}")
        for key in _lineage_keys(manifest, run):
            prior = splits.setdefault(key, split)
            if prior != split:
                raise ValueError(f"Shared project or task lineage crosses splits: {run} ({key[0]})")
        runs[run] = (run_dir, refs, artifacts, manifest, split, group)
    return runs


def _approvals(spec, selected):
    approvals = {}
    for item in spec.get("payload_allowlist", []):
        run, event, field = key = (item["run_id"], item["event_id"], item["field"])
        if (key in approvals or run not in selected or not isinstance(event, str) or field not in REF_FIELDS
                or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            raise ValueError("Invalid or duplicate payload approval for the selected purpose")
        approvals[key] = item["sha256"]
    return approvals


def _fit_row(record, budget):
    if len(encode(record)) <= budget:
        return record
    basic = {key: record["data"][key] for key in ("event_id", "type", "seq", "context_episode_id")
             if key in record["data"]}
    return {**record, "data": {**basic, "projection_status": "record_exceeds_window_budget"}}


def _order_within_source(left, right):
    if not left.get("source_id") or left.get("source_id") != right.get("source_id"):
        return None
    if left.get("native_session_id") != right.get("native_session_id"):
        return None
    before, after = left.get("seq"), right.get("seq")
    if type(before) is not int or type(after) is not int or before == after:
        return None
    return before < after


def _windows(run_id, manifest, split, group, use, records, budget):
    lineage = manifest.get("lineage_id") or identifier("lineage", group)
    origin = manifest.get("source_run_id") or run_id
    def is_boundary(row):
        return row["source"] == "events.jsonl" and row["data"].get("type") in _BOUNDARIES

    first_boundary = next((row["data"] for row in records if is_boundary(row)), {})
    boundary = None
    current, size, windows = [], 0, []
    history, history_bytes = deque(), 0
    for record in records:
        data = record["data"]
        event_id = data.get("event_id") if record["source"] == "events.jsonl" else None
        reused_prefix = False
        if event_id and is_boundary(record):
            if current and boundary:
                windows.append((boundary["event_id"], current))
                current, size = [], 0
            if boundary and use != "weight_training":
                for prior, _ in history:
                    if _order_within_source(prior["data"], data) is True:
                        prefix = {**prior, "phase": "before_boundary"}
                        current.append(prefix)
                        size += len(encode(prefix)) + 1
                reused_prefix = bool(current)
            boundary, phase = data, "boundary"
        elif event_id is None:
            phase = "offline_outcome" if record["source"] == "scores.jsonl" else "metadata"
        elif boundary and _order_within_source(boundary, data) is True:
            phase = "after_boundary"
        elif not boundary and _order_within_source(data, first_boundary) is True:
            phase = "before_boundary"
        else:
            phase = "unresolved"
        row = _fit_row({**record, "phase": phase}, budget - 1)
        width = len(encode(row)) + 1
        if current and size + width > budget:
            if reused_prefix:
                current, size = [], 0
            else:
                windows.append((None if phase == "boundary" else (boundary or {}).get("event_id"), current))
                current, size = [], 0
        current.append(row)
        size += width
        if event_id and width <= budget // 3:
            history.append((row, width))
            history_bytes += width
            while history_bytes > budget // 3:
                _, removed = history.popleft()
                history_bytes -= removed
    if current:
        windows.append(((boundary or {}).get("event_id"), current))
    result = []
    for index, (boundary_event, rows) in enumerate(windows):
        event_ids = list(dict.fromkeys(
            record["data"]["event_id"] for record in rows
            if record["source"] == "events.jsonl" and "event_id" in record["data"]
        ))
        records_text = "\n".join(encode(row).decode() for row in rows)
        result.append({
            "window_id": identifier("window", origin, split, event_ids[0] if event_ids else None, index),
            "revision": digest(encode([run_id, rows])),
            "lineage_id": lineage, "group": group, "split": split, "run_id": run_id,
            "event_refs": [{"run_id": run_id, "event_id": event_id} for event_id in event_ids],
            "event_ids": event_ids, "boundary_event_id": boundary_event,
            "visibility": "indexed_metadata_and_explicit_display_payloads", "allowed_use": [use],
            "records": records_text, "structure": rows,
        })
    for index, window in enumerate(result):
        window["previous_window"] = result[index - 1]["window_id"] if index else None
        window["next_window"] = result[index + 1]["window_id"] if index + 1 < len(result) else None
    return result


def _path(output):
    path = Path(output)
    return path if path.suffix == ".json" else path / "snapshot.json"


def _window_index(window):
    return {key: value for key, value in window.items() if key not in {"records", "structure"}} | {
        "bytes": len(window["records"].encode()), "sha256": digest(window["records"].encode()),
    }


def _result(document):
    index = {key: value for key, value in document.items() if key != "window_data"}
    index["snapshot_sha256"] = document["snapshot_digest"]
    windows = document["window_data"]
    event_refs = {(ref["run_id"], ref["event_id"]) for window in windows for ref in window["event_refs"]}
    return index, windows, event_refs


def create_snapshot(archive, spec, output, *, batch_dir=None, purpose="discovery"):
    archive = Path(archive).resolve(strict=True)
    if purpose not in _USES:
        raise ValueError(f"Unknown review purpose: {purpose}")
    use = _USES[purpose]
    allowed = spec.get("allowed_use", [use] if purpose != "training" else [])
    if not isinstance(allowed, list) or use not in allowed:
        raise ValueError(f"No explicit approval for {use}")
    authorization = spec.get("authorization_ref")
    if purpose == "training" and (not isinstance(authorization, str) or not authorization.strip()):
        raise ValueError("Weight training requires an authorization reference")
    plan, memberships = _split_plan(spec)
    index_data = inside(archive, "batch_index.json").read_bytes()
    index_sha = digest(index_data)
    if spec.get("batch_index_sha256", index_sha) != index_sha:
        raise ValueError("Archive differs from the approved batch digest")
    batch_index = json.loads(index_data)
    if batch_dir is None and batch_index.get("batch_local") and archive.parent.name == "derived":
        batch_dir = archive.parent.parent
    selected = {run for run, (split, _) in memberships.items() if split in _SPLITS[purpose]}
    if not selected:
        raise ValueError(f"No explicit runs selected for {purpose}")
    approvals = _approvals(spec, selected)
    runs = _freeze_memberships(archive, batch_index, memberships)
    budget = spec.get("window_bytes", 12_000)
    if type(budget) is not int or budget < 512:
        raise ValueError("window_bytes must be an integer of at least 512")
    scores_allowed = purpose != "training" and spec.get("allow_offline_scores") is True
    files = (*INDEX_FILES, "scores.jsonl") if scores_allowed else INDEX_FILES
    windows, input_hashes, errors, models = [], [], [], set()
    run_context = {}
    for run_id, (run_dir, refs, artifacts, manifest, split, group) in runs.items():
        if run_id not in selected:
            continue
        if purpose == "training":
            quality = json.loads(checked_bytes(run_dir, "quality.json", refs["quality.json"]))
            if "benchmark_evaluation_only" in quality.get("training_eligibility_reasons", ()):
                raise ValueError(f"Run {run_id} is scoped to benchmark evaluation")
        if manifest.get("model_id"):
            models.add(str(manifest["model_id"]))
        run_context[run_id] = {
            **{key: manifest.get(key) for key in (
                "task_name", "schedule", "seed", "job_attempt_id", "project_id", "harness_name", "harness_version",
                "source_run_id", "source_fingerprint", "source_content_sha256",
            )},
            "lineage_id": manifest.get("lineage_id") or identifier("lineage", group),
            "manifest_sha256": refs["manifest.json"]["sha256"],
            "initial_environment_id": _initial_environment(manifest),
        }
        records = []
        for path in files:
            if path not in refs and path != "artifacts.json":
                if path != "observations.json":
                    errors.append({"run_id": run_id, "path": path, "reason": "not_exposed"})
                continue
            content = artifacts if path == "artifacts.json" else checked_bytes(run_dir, path, refs[path])
            input_hashes.append({"run_id": run_id, "path": path, "sha256": digest(content),
                                 "bytes": len(content), "development_group": group})
            for number, doc in enumerate(documents(path, content)):
                row = metadata(doc)
                if path == "scores.jsonl":
                    row.update({key: doc[key] for key in ("raw_score", "score", "reward")
                                if type(doc.get(key)) in (int, float)})
                    row["visibility"] = "offline_score"
                if path == "events.jsonl" and doc.get("event_id"):
                    approved_payloads = {}
                    for field in sorted(REF_FIELDS):
                        key = (run_id, doc["event_id"], field)
                        if key in approvals:
                            training_state = (
                                purpose == "training" and doc.get("type") in {"decision_state", "training_state"}
                                and field in {"raw_ref", "source_ref"}
                            )
                            if purpose == "training" and not training_state:
                                raise ValueError("Training payload approval must target a decision-state source")
                            payload = allowed_payload(
                                batch_dir, run_dir, doc.get(field), approvals.pop(key), doc, training=training_state
                            )
                            if training_state and payload:
                                if "visible_state" in row and row["visible_state"] != payload:
                                    raise ValueError("Approved training state references disagree")
                                row["visible_state"] = payload
                                row["visible_state_source_ref"] = doc[field]
                            else:
                                approved_payloads[field] = payload
                    if approved_payloads:
                        row["approved_display_payloads"] = approved_payloads
                if row:
                    records.append({"source": path, "record_index": number, "data": row})
        windows.extend(_windows(run_id, manifest, split, group, use, records, budget))
    if approvals:
        raise ValueError("Approved payload references an unknown event")
    if not windows:
        raise ValueError("No visible records for the selected purpose")
    document = {
        "schema_version": SCHEMA, "purpose": purpose, "allowed_use": [use],
        "authorization_ref": authorization or f"review-spec-sha256:{digest(encode(spec))}",
        "source_run_refs": [run for run in memberships if run in selected],
        "lineage_ids": sorted({window["lineage_id"] for window in windows}),
        "selection_identities": [
            {"run_id": run_id, "lineage_id": context["lineage_id"],
             "manifest_sha256": context["manifest_sha256"], "source_fingerprint": context["source_fingerprint"],
             "source_content_sha256": context["source_content_sha256"],
             "initial_environment_id": context["initial_environment_id"]}
            for run_id, context in run_context.items()
        ],
        "input_hashes": input_hashes, "batch_index_sha256": index_sha,
        "input_allowlist": spec.get("payload_allowlist", []), "private_eval_exclusions": PRIVATE_EXCLUSIONS,
        "split_provenance": {"run_groups": plan, "development_groups": plan.get("development", {}),
                             "holdout_groups": list(plan.get("holdout", {}))},
        "offline_scores_allowed": scores_allowed, "executor_model_ids": sorted(models),
        "reading_coverage": [], "run_context": run_context, "missing_reasons": errors,
        "projection": "indexed_metadata_and_explicit_display_payloads",
        "windows": [_window_index(window) for window in windows],
        "unread_refs": [window["window_id"] for window in windows],
        "window_data": windows,
    }
    document["snapshot_id"] = identifier("snapshot", purpose, index_sha, plan, document["authorization_ref"])
    document["snapshot_digest"] = digest(encode(document))
    target = _path(output)
    content = encode(document) + b"\n"
    try:
        write_bytes(target, content, replace=False)
    except FileExistsError:
        if target.read_bytes() != content:
            raise ValueError(f"Review snapshot already exists with different data: {target}") from None
    return _result(document)


def load_snapshot(path, *, purpose="discovery"):
    document = read_json(_path(path))
    if document.get("schema_version") != SCHEMA:
        raise ValueError("Unsupported review snapshot schema")
    claimed = document.pop("snapshot_digest", None)
    if claimed != digest(encode(document)):
        raise ValueError("Review snapshot content differs from its digest")
    if document.get("purpose") != purpose or _USES.get(purpose) not in document.get("allowed_use", ()):
        raise ValueError(f"Review snapshot is not approved for {purpose}")
    if document["windows"] != [_window_index(window) for window in document["window_data"]]:
        raise ValueError("Review window index differs from the approved records")
    document["snapshot_digest"] = claimed
    return _result(document)
