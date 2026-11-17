"""Export CL Bench viewer artifacts using the official scoring helpers."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REFERENCE_PATH = Path(__file__).resolve().parent / "configs/reference_baselines.json"
LEADERBOARD_PATH = Path(__file__).resolve().parent / "configs/leaderboard_reference.json"


def _analysis_module(bench_root: Path) -> Any:
    source = bench_root / "scripts/analyze_final_results.py"
    if not source.is_file():
        raise ValueError(f"CL Bench analysis helper not found: {source}")
    sys.path.insert(0, str(bench_root))
    spec = importlib.util.spec_from_file_location("_clbench_export_analysis", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _outcomes(trace: dict[str, Any], label: str) -> list[dict[str, Any]]:
    outcomes = (trace.get("result") or {}).get("instance_outcomes") or []
    if not outcomes:
        raise ValueError(f"{label}: missing instance outcomes")
    ids = [str(item["instance_id"]) for item in outcomes]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{label}: duplicate instance IDs")
    if any(
        isinstance(item.get("reward"), bool)
        or not isinstance(item.get("reward"), (int, float))
        or not math.isfinite(item["reward"])
        for item in outcomes
    ):
        raise ValueError(f"{label}: missing or non-finite reward")
    status = (trace.get("execution") or {}).get("status", "completed")
    if status != "completed":
        raise ValueError(f"{label}: trace is {status}, not completed")
    return outcomes


def _pricing_complete(trace: dict[str, Any]) -> bool:
    usage = ((trace.get("execution") or {}).get("usage") or {}).get("interaction")
    return (
        isinstance(usage, dict)
        and usage.get("pricing_complete") is True
        and usage.get("unpriced_call_count", 0) == 0
        and isinstance(usage.get("cost_usd"), (int, float))
        and math.isfinite(usage["cost_usd"])
        and usage["cost_usd"] >= 0
    )


def _validate_task(
    task: str,
    artifact: dict[str, Any],
    reference: dict[str, Any],
    *,
    full: bool,
) -> tuple[list[str], set[int]]:
    summary = artifact.get("summary") or {}
    baseline = artifact.get("baseline_trace") or {}
    baseline_outcomes = _outcomes(baseline, f"{task} baseline")
    baseline_ids = {str(item["instance_id"]) for item in baseline_outcomes}
    runs = artifact.get("run_traces") or []
    if not runs:
        raise ValueError(f"{task}: missing stateful rollouts")
    if summary.get("baseline_mode", "reset_each_instance") != "reset_each_instance":
        raise ValueError(f"{task}: baseline must reset the system for every instance")
    if full:
        params = (summary.get("task") or {}).get("params") or {}
        schedule = summary.get("schedule") or params.get("schedule")
        if schedule != "default":
            raise ValueError(f"{task}: full export requires the default schedule")
        if len(baseline_outcomes) != reference["instances"]:
            raise ValueError(
                f"{task}: full baseline must have {reference['instances']} instances"
            )
        if len(runs) != 5:
            raise ValueError(f"{task}: full export requires five complete rollouts")
        if summary.get("run_mode") != reference["mode"]:
            raise ValueError(f"{task}: full export requires mode={reference['mode']}")
    warnings = []
    if not _pricing_complete(baseline):
        if full:
            raise ValueError(f"{task}: baseline pricing is incomplete")
        warnings.append("Baseline pricing incomplete; baseline spend is unavailable.")
    unpriced = set()
    indices = set()
    for position, item in enumerate(runs):
        index = int(item.get("run_index", position))
        if index in indices:
            raise ValueError(f"{task}: duplicate rollout index {index}")
        indices.add(index)
        trace = item.get("trace") or {}
        outcomes = _outcomes(trace, f"{task} rollout {index}")
        if {str(outcome["instance_id"]) for outcome in outcomes} != baseline_ids:
            raise ValueError(
                f"{task} rollout {index}: baseline/rollout instance sets differ"
            )
        if not _pricing_complete(trace):
            if full:
                raise ValueError(f"{task} rollout {index}: pricing is incomplete")
            unpriced.add(index)
    if unpriced:
        warnings.append("Rollout pricing incomplete; task cost is unavailable.")
    return warnings, unpriced


def _csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _published_comparison(
    local_row: dict[str, Any], reference: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshot = json.loads(LEADERBOARD_PATH.read_text())
    if snapshot["source_sha256"] != reference["source_sha256"]:
        raise ValueError("Published ranking snapshot and normalization reference differ")
    rows = []
    for entry in snapshot["leaderboard"]:
        score = entry.get("normalized_reward_mean")
        if (
            entry.get("tasks_included") == len(reference["tasks"])
            and isinstance(score, (int, float))
            and math.isfinite(score)
        ):
            rows.append({
                **entry,
                "origin": "published",
                "published_rank": entry.get("rank"),
                "tasks_completed": entry["tasks_included"],
            })
    rows.append(local_row)
    rows.sort(key=lambda entry: -entry["normalized_reward_mean"])
    for rank, entry in enumerate(rows, 1):
        entry["rank"] = rank
    comparison = {
        key: snapshot[key]
        for key in (
            "source_url", "source_last_updated", "retrieved_on", "source_sha256",
            "normalization",
        )
    }
    comparison["rank_scope"] = "local_estimate_against_published_snapshot"
    comparison["submitted"] = False
    return rows, comparison


def export_results(
    bench_root: Path,
    run_dir: Path,
    profile: str | None = None,
) -> dict[str, Any]:
    """Write leaderboard_data.json and two CSVs; never run or call a model.

    ``full`` requires the entire official default evaluation and estimates rank
    against the pinned published summaries. ``quick`` exports raw task metrics
    and curves; normalized metrics and rank remain unavailable.
    """
    bench_root, run_dir = Path(bench_root).resolve(), Path(run_dir).resolve()
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    profile = profile or manifest.get("profile", "quick")
    if profile not in {"quick", "full"}:
        raise ValueError("profile must be quick or full")
    full = profile == "full"
    run_name = manifest.get("run_name") or run_dir.name
    reference = json.loads(REFERENCE_PATH.read_text())
    task_references = reference["tasks"]
    analysis = _analysis_module(bench_root)
    artifacts = analysis.discover_task_artifacts(run_dir, run_dir.parent)
    if not artifacts:
        raise ValueError(f"No CL Bench viewer artifacts found in {run_dir}/tasks")
    if full and set(artifacts) != set(task_references):
        raise ValueError("Full export requires exactly the six official tasks")
    task_summaries, run_points, warnings = [], [], {}
    baseline_spend = {}
    for task, artifact in sorted(artifacts.items()):
        if task not in task_references:
            raise ValueError(f"Unknown task in pinned leaderboard reference: {task}")
        task_warnings, unpriced = _validate_task(
            task, artifact, task_references[task], full=full
        )
        points, collector_warnings = analysis.collect_task_points(
            run_name,
            artifact,
            task,
            task_references[task]["baseline_reward"],
            {key: value["r_max"] for key, value in task_references.items()},
        )
        task_warnings.extend(collector_warnings)
        if len(points) != len(artifact["run_traces"]):
            raise ValueError(f"{task}: official analysis excluded an incomplete rollout")
        for point in points:
            if full and (
                point.normalized_reward is None or point.normalized_gain is None
            ):
                raise ValueError(f"{task}: a normalization denominator is zero")
            if not full:
                point.normalized_reward = point.normalized_gain = None
            if unpriced:
                point.cost_usd = None
                point.cost_curve = []
        task_summaries.append(analysis.summarize_task(points, task_warnings))
        run_points.extend(points)
        if task_warnings:
            warnings[task] = task_warnings
        baseline_trace = artifact["baseline_trace"]
        baseline_spend[task] = (
            analysis.usage_cost(baseline_trace.get("execution"))
            if _pricing_complete(baseline_trace)
            else None
        )
    rows = analysis.aggregate_leaderboard(task_summaries)
    row = rows[0]
    row["rank"] = None
    row["published_rank"] = None
    row["origin"] = "local"
    row["tasks_completed"] = len(task_summaries)
    row["display_name"] = manifest.get("display_name") or run_name
    row["system_label"] = "DSH"
    row["system_class"] = "dsh"
    row["model"] = (manifest.get("system") or {}).get("model", "")
    if not full:
        row["normalized_reward_mean"] = row["normalized_gain_mean"] = None
    if any(task.cost_mean is None for task in task_summaries):
        row["total_cost_usd_mean"] = row["mean_task_cost_usd"] = None
    comparison = None
    if full:
        rows, comparison = _published_comparison(row, reference)
    payload = {
        "last_updated": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
        "profile": profile,
        "leaderboard_comparable": full,
        "rank_scope": (
            "local_estimate_against_published_snapshot" if full else "unranked_quick"
        ),
        "local_estimated_rank": row["rank"],
        "submitted": False,
        "comparison": comparison,
        "leaderboard": rows,
        "task_summary": [analysis.as_row(item) for item in task_summaries],
        "run_points": [analysis.as_row(item) for item in run_points],
        "warnings": warnings,
        "normalization": {
            "reward": (
                "(reward - fixed icl-gpt-5.4 stateless baseline) / "
                "(reference_max - fixed baseline)"
            ),
            "gain": (
                "(reward - own stateless baseline) / "
                "(reference_max - own baseline)"
            ),
            "aggregation": (
                "Equal mean across six tasks; cost sums mean single-rollout "
                "task costs, excluding baseline."
            ),
            "quick": "Normalized metrics unavailable for quick runs.",
        },
        "reference": reference,
        "provenance": {
            "analysis_helper": str(bench_root / "scripts/analyze_final_results.py"),
            "analysis_sha256": hashlib.sha256(
                (bench_root / "scripts/analyze_final_results.py").read_bytes()
            ).hexdigest(),
        },
        "baseline_cost_usd": baseline_spend,
    }
    (run_dir / "leaderboard_data.json").write_text(
        json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    _csv(
        run_dir / "summary.csv",
        rows,
        [
            "rank", "run_name", "display_name", "tasks_completed",
            "normalized_reward_mean", "normalized_gain_mean", "total_cost_usd_mean",
            "origin", "published_rank",
        ],
    )
    _csv(
        run_dir / "task_summary.csv",
        payload["task_summary"],
        [
            "task", "run_name", "n_runs", "n_instances", "reward_mean", "reward_se",
            "gain_mean", "gain_se", "cost_mean", "cost_se", "latency_mean",
            "latency_se", "normalized_reward_mean", "normalized_gain_mean", "warnings",
        ],
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench-root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--profile", choices=("quick", "full"))
    args = parser.parse_args()
    try:
        payload = export_results(args.bench_root, args.run_dir, args.profile)
    except (ValueError, KeyError) as exc:
        parser.exit(1, f"Export failed: {exc}\n")
    print(f"Exported {len(payload['task_summary'])} tasks to {args.run_dir}")


if __name__ == "__main__":
    main()
