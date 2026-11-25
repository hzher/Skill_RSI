"""Freeze treatment, independent allocation and release criteria before execution."""
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

from ..components import ComponentRegistry
from ..evidence.io import encode, identifier, read_json, write_bytes
from ..review.snapshot import load_snapshot

SCHEMA = "skill-rsi.experiment.v1"


def number(value):
    return type(value) in (int, float) and isfinite(value)


def _number_at_least(value, minimum, name):
    if not number(value) or value < minimum:
        raise ValueError(f"invalid {name}")
    return value


def _identities(index):
    rows = index.get("run_context", {})
    fields = ("lineage_id", "project_id", "source_run_id", "source_fingerprint", "source_content_sha256",
              "initial_environment_id", "manifest_sha256", "job_attempt_id")
    return {("run_id", run) for run in rows} | {
        (field, row[field]) for row in rows.values() for field in fields if row.get(field)
    }


def _scope(snapshot, discovery):
    approved, windows, _ = load_snapshot(snapshot, purpose="evaluation")
    lineages = {}
    for window in windows:
        run = approved["run_context"][window["run_id"]]
        lineages.setdefault(window["lineage_id"], {})[window["run_id"]] = run
    if not lineages:
        raise ValueError("holdout approval does not name any independent lineages")
    if discovery:
        development = read_json(discovery / "input_index.json")
        if not development.get("lineage_ids") or _identities(development) & _identities(approved):
            raise ValueError("evaluation lineages cannot be proven independent of discovery")
        plan = development.get("split_provenance", {}).get("run_groups", {}).get("holdout", {})
        declared = {run for runs in plan.values() if runs for run in runs}
        if not set(approved["source_run_refs"]) <= declared:
            raise ValueError("evaluation runs were not frozen as holdouts before discovery")
    return approved, lineages, {window["run_id"] for window in windows}


def freeze(output: Path, spec: dict, benchmark_fingerprint: str, snapshot: Path, *,
           discovery: Path | None = None, state_dir=None):
    approved, lineages, holdout_runs = _scope(snapshot, discovery)
    registry = ComponentRegistry(state_dir)
    conditions = {}
    for condition, config in spec["conditions"].items():
        identifier(condition)
        releases = []
        ids = set()
        for item in config.get("components", []):
            release = registry.resolve(item["component_id"], item["version"], item.get("form"))
            if item["component_id"] in ids:
                raise ValueError("a condition cannot bind two releases of the same component")
            ids.add(item["component_id"])
            releases.append({"component_id": item["component_id"], "version": item["version"],
                             "form": release.manifest["form"], "digest": release.digest,
                             "options": item.get("options", {})})
        conditions[condition] = {"components": releases, "baseline": config.get("baseline", "adapter")}
        if conditions[condition]["baseline"] not in {"adapter", "native", "model_tools"}:
            raise ValueError("baseline must be adapter, native or model_tools")
        if releases and conditions[condition]["baseline"] != "adapter":
            raise ValueError("a pure baseline cannot enable automatic components")
    if len(conditions) < 2:
        raise ValueError("a comparison needs at least two conditions")
    pairs, allocations = {}, set()
    for row in spec["pairs"]:
        key = identifier(row["pair_id"])
        for field in ("task_id", "lineage_id", "host", "host_version", "model", "scorer_version"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"pair requires {field}")
        if type(row.get("seed")) is not int:
            raise ValueError("pair seed must be an integer")
        allocation = row["task_id"], row["host"], row["host_version"], row["model"], row["seed"]
        if key in pairs or allocation in allocations or row["lineage_id"] not in lineages:
            raise ValueError("pair must be unique and belong to the approved holdout")
        records = lineages[row["lineage_id"]].values()
        if not any(record.get("task_name") in {None, row["task_id"]}
                   and record.get("seed") in {None, row["seed"]} for record in records):
            raise ValueError("pair task or seed does not match its holdout lineage")
        pairs[key] = {k: row[k] for k in ("task_id", "lineage_id", "host", "host_version", "model", "scorer_version", "seed")}
        allocations.add(allocation)
    if not pairs:
        raise ValueError("empty pair allocation")
    if any(item["baseline"] == "model_tools" for item in conditions.values()) and any(
        item["host"] != "dsh" for item in pairs.values()
    ):
        raise ValueError("model-controlled history tools require a DSH experiment")
    comparisons = []
    comparison_keys = set()
    for item in spec["comparisons"]:
        control, treatment = item["control"], item["treatment"]
        if control == treatment or not {control, treatment} <= conditions.keys():
            raise ValueError("invalid comparison conditions")
        if (control, treatment) in comparison_keys:
            raise ValueError("duplicate comparison")
        comparison_keys.add((control, treatment))
        minimum_pairs = item["minimum_pairs"]
        if type(minimum_pairs) is not int or not 1 <= minimum_pairs <= len(pairs):
            raise ValueError("invalid minimum pair count")
        metrics = {}
        for name, metric in item["metrics"].items():
            if not isinstance(name, str) or not name or metric.get("direction") not in {"higher", "lower"}:
                raise ValueError("each metric requires a name and orientation")
            threshold = metric["minimum_mean_delta"]
            if not number(threshold):
                raise ValueError("metric threshold must be finite")
            metrics[name] = {"direction": metric["direction"], "minimum_mean_delta": threshold}
        if item.get("primary") not in metrics:
            raise ValueError("each comparison requires a primary metric from its frozen criteria")
        comparisons.append({"control": control, "treatment": treatment, "metrics": metrics,
                            "primary": item["primary"], "minimum_pairs": minimum_pairs})
    if not comparisons:
        raise ValueError("no primary comparison")
    budget = spec["budget"]
    for key in ("max_steps", "wall_seconds"):
        if type(budget.get(key)) is not int or budget[key] <= 0:
            raise ValueError(f"budget requires positive integer {key}")
    if "max_tokens" in budget:
        _number_at_least(budget["max_tokens"], 1, "token budget")
    costs = {}
    for name, limit in spec.get("max_extra_cost", {}).items():
        costs[name] = _number_at_least(limit, 0, f"extra {name} limit")
    protocol = {"schema": SCHEMA, "protocol_id": identifier(spec["protocol_id"]),
                "frozen_at": datetime.now(timezone.utc).isoformat(), "benchmark_sha256": benchmark_fingerprint,
                "holdout_snapshot": approved.get("snapshot_digest"), "holdout_run_ids": sorted(holdout_runs),
                "conditions": conditions, "pairs": pairs, "comparisons": comparisons, "budget": budget,
                "max_extra_cost": costs, "host_options": spec.get("host_options", {})}
    payload = encode(protocol)
    write_bytes(output, payload + b"\n", replace=False)
    return protocol
