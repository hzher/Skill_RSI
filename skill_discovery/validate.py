import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev

from .artifacts import SCHEMA, checked_bytes, digest, encode, identifier, indexed_run, inside, read_json, read_ref, write_bytes, write_json


MATCH = ("task_name", "schedule", "seed", "phase", "mode", "harness_name", "model_id", "scorer_version")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _jsonl(path):
    return [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def freeze_validation(extraction: Path, protocol_id: str, spec: dict):
    protocol_id = identifier(protocol_id)
    candidate = next((item for item in _jsonl(extraction / "candidates.jsonl")
                      if item.get("skill_id") == spec["skill_id"] and item.get("version") == spec["version"]
                      and item.get("candidate_status") == "candidate"), None)
    if candidate is None:
        raise ValueError("validation requires a candidate with a passed static review")
    skill_path = inside(extraction, candidate["skill_path"])
    if digest(skill_path.read_bytes()) != candidate["skill_sha256"]:
        raise ValueError("skill hash differs from the extraction candidate")

    inputs = read_json(extraction / "input_index.json")
    scope = spec["scope"]
    group_id = identifier(spec["group_id"])
    group_field = {"development": "development_groups", "holdout": "holdout_groups"}.get(scope)
    if not group_field or group_id not in inputs["split_provenance"][group_field]:
        raise ValueError("validation group was not declared for this scope before teacher reading")
    pairs = {}
    for item in spec["pairs"]:
        pair_id = identifier(item["pair_id"])
        if pair_id in pairs:
            raise ValueError("duplicate pair in protocol")
        task = identifier(item["task_name"])
        match = item.get("match", {})
        if not isinstance(match, dict) or set(match).difference(MATCH):
            raise ValueError("invalid pinned match fields")
        pairs[pair_id] = {"task_name": task, "match": match}
    if not pairs:
        raise ValueError("freeze at least one validation pair")

    tasks = set(pair["task_name"] for pair in pairs.values())
    if scope == "holdout":
        seen = [run.get("task_name") for run in inputs["run_context"].values()]
        if not seen or None in seen or tasks.intersection(seen):
            raise ValueError("clean holdout requires known development tasks and disjoint validation tasks")
    targets = {}
    if set(spec["targets"]) != tasks:
        raise ValueError("freeze one task-specific target for every validation task")
    for task, target in spec["targets"].items():
        minimum_pairs = target["minimum_pairs"]
        if (type(minimum_pairs) is not int or minimum_pairs < 2
                or minimum_pairs > sum(pair["task_name"] == task for pair in pairs.values())):
            raise ValueError(f"invalid repeated-pair requirement for {task}")
        minimum_delta = target["minimum_mean_delta"]
        if not _number(minimum_delta) or target["direction"] not in {"higher", "lower"}:
            raise ValueError(f"invalid improvement target for {task}")
        targets[task] = {"metric_name": identifier(target["metric_name"]), "direction": target["direction"],
                         "minimum_pairs": minimum_pairs, "minimum_mean_delta": minimum_delta}
    protocol = {
        "schema_version": SCHEMA, "protocol_id": protocol_id, "scope": scope, "group_id": group_id,
        "candidate_id": candidate["candidate_id"], "skill_id": candidate["skill_id"],
        "version": candidate["version"], "skill_path": candidate["skill_path"],
        "skill_sha256": candidate["skill_sha256"], "pairs": pairs, "targets": targets,
        "development_run_ids": inputs["source_run_refs"], "adoption_required": spec.get("adoption_required", True),
        "development_attempt_ids": [record["job_attempt_id"] for record in inputs["run_context"].values()
                                    if record.get("job_attempt_id")],
        "reward_aggregation": "mean_within_same_official_instance_sequence",
        "validation_status": "not_tested", "frozen_at": datetime.now(timezone.utc).isoformat(),
    }
    if type(protocol["adoption_required"]) is not bool:
        raise ValueError("adoption_required must be boolean")
    root = extraction / "validation" / protocol_id
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / "protocol.json", protocol)
    return protocol


def _get_arm(extraction, arm):
    archive = Path(arm["archive"]).resolve()
    if (archive.parent.name, archive.name) != ("derived", "skill-rsi-v1"):
        raise ValueError("validation arms must use a batch-local evidence archive")
    batch = archive.parent.parent
    run_id = identifier(arm["run_id"])
    root, index, inventory = indexed_run(archive, run_id)
    manifest_data = checked_bytes(root, "manifest.json", index["manifest.json"])
    manifest = json.loads(manifest_data)
    if manifest.get("run_id") != run_id:
        raise ValueError("validation run ID does not match its manifest")
    batch_manifest_data = inside(batch, "manifest.json").read_bytes()
    batch_manifest = json.loads(batch_manifest_data)
    source_manifests = [source["location"] for source in manifest["sources"] if source["kind"] == "batch_manifest"]
    if len(source_manifests) != 1 or digest(batch_manifest_data) != source_manifests[0]["sha256"]:
        raise ValueError("validation batch manifest differs from archived provenance")
    records = {}
    hashes = {}
    for path in ("scores.jsonl", "events.jsonl", "context_episodes.jsonl", "requests/index.jsonl"):
        data = checked_bytes(root, path, index[path])
        records[path] = [json.loads(line) for line in data.splitlines() if line.strip()]
        hashes[path] = digest(data)
    refs = {
        "archive": os.path.relpath(archive, extraction), "run_id": run_id,
        "manifest_sha256": digest(manifest_data), "batch_manifest_sha256": digest(batch_manifest_data),
        "artifacts_sha256": digest(inventory), "record_hashes": hashes,
    }
    return manifest, batch_manifest, records, refs, batch, root


def _skill_loaded(events, sha256):
    refs = []
    for event in events:
        body = event.get("data") or event.get("payload") or event
        kind = event.get("boundary") or event.get("native_type") or body.get("kind") or event.get("type")
        if kind == "skill_loaded" and body.get("sha256", event.get("skill_sha256")) == sha256:
            if event.get("event_id"):
                refs.append(event["event_id"])
    return refs


def _prompt_exposure(batch, run, requests, skill_text):
    for row in requests:
        ref = row.get("request_ref")
        if ref and ref.get("payload_layer") == "native_options":
            actual = read_ref(batch, run, ref)
            if not isinstance(actual, dict):
                continue
            system = [
                block["text"] for message in actual.get("messages", [])
                if isinstance(message, dict) and message.get("role") == "system"
                for block in message.get("content", []) if isinstance(block, dict)
                and block.get("type") == "text" and isinstance(block.get("text"), str)
            ]
            if isinstance(actual.get("systemPrompt"), str):
                system.append(actual["systemPrompt"])
            if any(skill_text.strip() in section for section in system):
                return {"logical_call_id": row.get("logical_call_id"), "request_ref": ref,
                        "boundary": "native_options"}
    return None


def _adoption(episodes, requested, sha256):
    return [
        {"episode_id": episode["context_episode_id"], "event_ids": item["source_event_ids"]}
        for episode in episodes
        if episode.get("context_episode_id") in requested and episode.get("evidence_status") == "observed"
        for item in episode.get("lifecycle_refs", [])
        if item.get("stage") == "used" and item.get("source_event_ids")
        and (item.get("artifact_ref") or {}).get("sha256") == sha256
    ]


def _scores(rows, metric):
    matched = [row for row in rows if row.get("metric_name") == metric]
    matched.sort(key=lambda row: (row.get("instance_index") is None, row.get("instance_index") or 0))
    return [
        {"instance_id": row.get("instance_id"), "instance_index": row.get("instance_index"),
         "status": row.get("grading_status"), "value": row.get("raw_score"),
         "scorer_version": row.get("scorer_version")}
        for row in matched
    ]


def _pair(extraction, protocol, pair_id, spec, arm_refs, used):
    base, base_batch, base_records, base_ref, _, _ = _get_arm(extraction, arm_refs["without"])
    skill, skill_batch, skill_records, skill_ref, skill_path, skill_run = _get_arm(extraction, arm_refs["with"])
    target = protocol["targets"][spec["task_name"]]
    outcome = {"pair_id": pair_id, "task_name": spec["task_name"], "without": base_ref,
               "with": skill_ref, "reasons": [], "metric_name": target["metric_name"]}
    reasons = outcome["reasons"]
    for manifest in (base, skill):
        run_id = manifest["run_id"]
        attempt = manifest.get("job_attempt_id")
        if (run_id in used or run_id in protocol["development_run_ids"] or not attempt or attempt in used
                or attempt in protocol["development_attempt_ids"]):
            reasons.append("runs_must_be_fresh_independent_attempts")
        used.update((run_id, attempt))
        if (not manifest.get("created_at")
                or datetime.fromisoformat(manifest["created_at"]) < datetime.fromisoformat(protocol["frozen_at"])):
            reasons.append("run_must_start_after_protocol_freeze")
    if base_ref["archive"] == skill_ref["archive"]:
        reasons.append("arms_must_have_independent_batch_names")
    if not base_batch.get("comparison_fingerprint") or (
        base_batch["comparison_fingerprint"] != skill_batch.get("comparison_fingerprint")
    ):
        reasons.append("unverified_or_unmatched_executor_configuration")
    task = spec["task_name"]
    base_task = base_batch.get("tasks", {}).get(task, {})
    skill_task = skill_batch.get("tasks", {}).get(task, {})
    if not base_task.get("max_workers") or base_task["max_workers"] != skill_task.get("max_workers"):
        reasons.append("unverified_or_unmatched_worker_count")
    for key in MATCH:
        if base.get(key) != skill.get(key):
            reasons.append(f"unmatched_{key}")
    for key, value in spec["match"].items():
        if base.get(key) != value:
            reasons.append(f"unexpected_{key}")
    if base.get("task_name") != spec["task_name"] or base.get("phase") in {"baseline", None}:
        reasons.append("requires_matched_stateful_or_rollout_scoring")
    required_sha = protocol["skill_sha256"]
    baseline_declared = base_batch.get("skill_loading")
    treated_declared = skill_batch.get("skill_loading")
    load_refs = _skill_loaded(skill_records["events.jsonl"], required_sha)
    if not isinstance(baseline_declared, dict) or baseline_declared.get("sha256") is not None:
        reasons.append("baseline_skill_state_not_proven")
    if not isinstance(treated_declared, dict) or treated_declared.get("sha256") != required_sha or not load_refs:
        reasons.append("treatment_skill_loading_not_proven")
    skill_text = inside(extraction, protocol["skill_path"]).read_text(encoding="utf-8")
    exposure = _prompt_exposure(skill_path, skill_run, skill_records["requests/index.jsonl"], skill_text)
    if not exposure:
        reasons.append("skill_prompt_exposure_not_proven")
    adoption_refs = _adoption(skill_records["context_episodes.jsonl"],
                              arm_refs["with"].get("adoption_episode_ids", []), required_sha)
    outcome.update({"skill_load_event_ids": load_refs, "skill_prompt_exposure": exposure, "adoption_refs": adoption_refs,
                    "adoption_status": "observed" if adoption_refs else "unknown"})
    if protocol["adoption_required"] and not adoption_refs:
        reasons.append("skill_adoption_not_proven")

    a = _scores(base_records["scores.jsonl"], target["metric_name"])
    b = _scores(skill_records["scores.jsonl"], target["metric_name"])
    outcome.update({"without_scores": a, "with_scores": b})
    keys = ("instance_index", "instance_id", "scorer_version")
    if not a or [tuple(row[key] for key in keys) for row in a] != [tuple(row[key] for key in keys) for row in b]:
        reasons.append("official_instance_score_sequence_unmatched")
    if (len(a) != base.get("expected_instance_count") or len(b) != skill.get("expected_instance_count")
            or len(a) != len({row["instance_index"] for row in a})):
        reasons.append("incomplete_official_score_sequence")
    if any(row["status"] != "valid" or not _number(row["value"]) for row in a + b):
        reasons.append("unscorable_or_non_numeric_reward")
    outcome["quality"] = {"without_scorable": sum(row["status"] == "valid" for row in a), "without_total": len(a),
                          "with_scorable": sum(row["status"] == "valid" for row in b), "with_total": len(b)}
    outcome["execution"] = {"without": base.get("status"), "with": skill.get("status")}
    if any(manifest.get("status") != "finished" for manifest in (base, skill)):
        reasons.append("run_termination_not_finished")
    outcome["cost"] = {"without": base.get("usage"), "with": skill.get("usage")}
    outcome["benefit"] = None
    if a and b and len(a) == len(b) and not any(
        reason in reasons for reason in ("official_instance_score_sequence_unmatched", "unscorable_or_non_numeric_reward")
    ):
        raw_delta = mean(y["value"] - x["value"] for x, y in zip(a, b))
        outcome["raw_delta"] = raw_delta
        if not reasons:
            outcome["benefit"] = raw_delta if target["direction"] == "higher" else -raw_delta
    return outcome


def evaluate_validation(extraction: Path, protocol_id: str, results: dict):
    root = inside(extraction, f"validation/{identifier(protocol_id)}")
    path = inside(root, "protocol.json")
    protocol = read_json(path)
    if digest(inside(extraction, protocol["skill_path"]).read_bytes()) != protocol["skill_sha256"]:
        raise ValueError("validated skill has changed since protocol freeze")
    provided, pairs, used = {}, [], set()
    for item in results["pairs"]:
        pair_id = item["pair_id"]
        if pair_id in provided or pair_id not in protocol["pairs"]:
            raise ValueError("result refers to an unknown or repeated pair")
        provided[pair_id] = item
    for pair_id, spec in protocol["pairs"].items():
        if pair_id not in provided:
            pairs.append({"pair_id": pair_id, "task_name": spec["task_name"], "benefit": None,
                          "reasons": ["pair_not_run"]})
            continue
        try:
            pairs.append(_pair(extraction, protocol, pair_id, spec, provided[pair_id], used))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            pairs.append({"pair_id": pair_id, "task_name": spec["task_name"], "benefit": None,
                          "reasons": ["invalid_run_evidence"], "error_type": type(exc).__name__})
    tasks = {}
    for task, target in protocol["targets"].items():
        rows = [row for row in pairs if row["task_name"] == task]
        deltas = [row["benefit"] for row in rows if row["benefit"] is not None]
        avg = mean(deltas) if deltas else None
        deviation = stdev(deltas) if len(deltas) >= 2 else None
        status = "inconclusive"
        if len(deltas) >= target["minimum_pairs"] and len(deltas) == len(rows):
            status = "supported" if avg >= target["minimum_mean_delta"] else "rejected"
        tasks[task] = {
            "validation_status": status, "metric_name": target["metric_name"],
            "matched_pairs": len(deltas), "planned_pairs": len(rows), "mean_oriented_delta": avg,
            "sample_standard_deviation": deviation,
            "standard_error": deviation / math.sqrt(len(deltas)) if deviation is not None else None,
            "regression_pair_ids": [row["pair_id"] for row in rows if row["benefit"] is not None and row["benefit"] < 0],
        }
    statuses = {row["validation_status"] for row in tasks.values()}
    status = "supported" if statuses == {"supported"} else "rejected" if "rejected" in statuses else "inconclusive"
    output = {
        "schema_version": SCHEMA, "protocol_id": protocol_id, "protocol_sha256": digest(path.read_bytes()),
        "candidate_id": protocol["candidate_id"], "skill_sha256": protocol["skill_sha256"],
        "candidate_status": {"supported": "validated_in_scope", "rejected": "rejected",
                             "inconclusive": "needs_more_evidence"}[status],
        "validation_status": status, "pairs": pairs, "task_results": tasks,
        "generalization_scope": {
            "split": protocol["scope"], "group_id": protocol["group_id"],
            "supported_tasks": sorted(task for task, row in tasks.items() if row["validation_status"] == "supported"),
            "unmeasured_scope": "unknown",
        },
    }
    write_bytes(root / "result.json", encode(output) + b"\n", replace=False)
    return output
