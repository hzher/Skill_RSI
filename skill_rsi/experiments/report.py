"""Compare completed official score sequences while retaining the full planned denominator."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from math import sqrt
from pathlib import Path
from statistics import mean, stdev

from ..evidence.io import digest, encode, inside, read_json, write_bytes
from .protocol import SCHEMA
from .run import metrics


def _load(protocol_path, root, protocol):
    index = read_json(root / "execution.json")
    sha = digest(protocol_path.read_bytes())
    if index["protocol_sha256"] != sha:
        raise ValueError("execution does not match the frozen protocol")
    arms, attempts = {}, set()
    for row in index["arms"]:
        key = row["pair_id"], row["condition"]
        if key in arms or key[0] not in protocol["pairs"] or key[1] not in protocol["conditions"]:
            raise ValueError("unknown or duplicate arm receipt")
        raw = inside(root, row["path"]).read_bytes()
        if digest(raw) != row["sha256"]:
            raise ValueError("arm receipt digest changed")
        arm = json.loads(raw)
        if ((arm["pair_id"], arm["condition"]) != key or arm["attempt_id"] in attempts
                or arm["match"] != protocol["pairs"][key[0]]
                or arm["assigned_components"] != protocol["conditions"][key[1]]["components"]
                or datetime.fromisoformat(arm["started_at"]) < datetime.fromisoformat(protocol["frozen_at"])):
            raise ValueError("arm does not match its independent assignment")
        attempts.add(arm["attempt_id"])
        arms[key] = arm
    return arms, sha


def _score_keys(arm):
    return [(row["instance_id"], row["instance_index"]) for row in arm["objective"]["instance_outcomes"]]


def _delivered(arm):
    def applied(receipt):
        return receipt.get("status") == "applied" and receipt.get("stage") in {"delivered", "applied"}

    return {row["artifact_digest"] for row in arm.get("decisions", ()) if row.get("mode") == "apply"
            and (applied(row) or any(applied(receipt) for receipt in row.get("receipts", ())))}


def _compare_pair(left, right, requirements, pair):
    if left is None or right is None:
        return {"reason": "arm_not_run"}
    if any(arm["status"] != "completed" for arm in (left, right)):
        return {"reason": "incomplete_arm", "arms": [left["status"], right["status"]]}
    if any(arm["objective"]["scorer_version"] != pair["scorer_version"] for arm in (left, right)):
        return {"reason": "scorer_version_mismatch"}
    left_ids, right_ids = _score_keys(left), _score_keys(right)
    if not left_ids or left_ids != right_ids or len(set(left_ids)) != len(left_ids):
        return {"reason": "official_score_sequence_mismatch"}
    observed = {"control": sorted(_delivered(left)), "treatment": sorted(_delivered(right))}
    absent = {side: sorted({row["digest"] for row in arm["assigned_components"]} - set(observed[side]))
              for side, arm in (("control", left), ("treatment", right))}
    first, second = metrics(left), metrics(right)
    required = set(requirements["metrics"])
    if not required <= first.keys() & second.keys():
        return {"reason": "unscored_required_metric", "missing_metrics": sorted(required - (first.keys() & second.keys())),
                "missing_deliveries": absent}
    for name, spec in requirements["metrics"].items():
        for arm in (left, right):
            native = [item.get("raw_metric_higher_is_better") for item in arm["objective"]["instance_outcomes"]
                      if item.get("raw_metric_name") == name]
            if native and any(direction is not (spec["direction"] == "higher") for direction in native):
                return {"reason": "metric_orientation_mismatch", "metric": name}
    delta = {name: (second[name] - first[name]) * (1 if spec["direction"] == "higher" else -1)
             for name, spec in requirements["metrics"].items()}
    overhead = {name: second[name] - first[name] for name in left["usage"].keys() & right["usage"].keys()}
    return {"delta": delta, "cost_delta": overhead, "missing_deliveries": absent, "delivered": observed,
            "instance_count": len(left_ids)}


def _outcome(comparison, rows, limits, *, has_treatment):
    per_metric = {}
    for name, target in comparison["metrics"].items():
        deltas = [row["delta"][name] for row in rows if "delta" in row]
        sd = stdev(deltas) if len(deltas) >= 2 else None
        per_metric[name] = {"count": len(deltas), "mean_delta": mean(deltas) if deltas else None,
                            "standard_error": sd / sqrt(len(deltas)) if sd is not None else None,
                            "regressed_pair_ids": [row["pair_id"] for row in rows
                                                   if "delta" in row and row["delta"][name] < 0],
                            "minimum_mean_delta": target["minimum_mean_delta"]}
    comparable = per_metric[comparison["primary"]]["count"]
    costs = {}
    for name, limit in limits.items():
        values = [row["cost_delta"][name] for row in rows if name in row.get("cost_delta", {})]
        costs[name] = {"count": len(values), "mean_extra": mean(values) if values else None, "maximum": limit}
    if (comparable < comparison["minimum_pairs"] or comparable != len(rows)
            or any(cost["count"] != comparable for cost in costs.values())):
        status = "inconclusive"
    elif (any(item["mean_delta"] < item["minimum_mean_delta"] for item in per_metric.values())
          or any(item["mean_extra"] > item["maximum"] for item in costs.values())):
        status = "rejected"
    elif has_treatment and not any(
        "missing_deliveries" in row and not any(row["missing_deliveries"].values()) for row in rows
    ):
        status = "inconclusive"
    else:
        status = "validated_in_scope" if has_treatment else "measured"
    reasons = Counter(row["reason"] for row in rows if "reason" in row)
    if any(any(row.get("missing_deliveries", {}).values()) for row in rows):
        reasons["frozen_components_without_delivery"] = sum(
            any(row.get("missing_deliveries", {}).values()) for row in rows)
    return {**comparison, "status": status, "planned_pairs": len(rows), "comparable_pairs": comparable,
            "denominator_notes": dict(reasons), "statistics": per_metric, "costs": costs, "pairs": rows}


def report(protocol_path: Path, execution: Path, output: Path):
    protocol = read_json(protocol_path)
    if protocol.get("schema") != SCHEMA:
        raise ValueError("unknown experiment protocol schema")
    arms, sha = _load(protocol_path, execution, protocol)
    comparisons = []
    for comparison in protocol["comparisons"]:
        control, treatment = comparison["control"], comparison["treatment"]
        groups = {}
        for pair_id, pair in protocol["pairs"].items():
            scope = tuple(pair[key] for key in ("task_id", "host", "host_version", "model", "scorer_version"))
            compared = _compare_pair(arms.get((pair_id, control)), arms.get((pair_id, treatment)), comparison, pair)
            groups.setdefault(scope, []).append({"pair_id": pair_id, **compared})
        for scope, rows in groups.items():
            condition = protocol["conditions"][treatment]
            result = _outcome(comparison, rows, protocol["max_extra_cost"], has_treatment=bool(condition["components"]))
            result["condition_config"] = {name: protocol["conditions"][name] for name in (control, treatment)}
            result["scope"] = dict(zip(("task_id", "host", "host_version", "model", "scorer_version"), scope))
            result["artifact_digests"] = [row["digest"] for row in protocol["conditions"][treatment]["components"]]
            comparisons.append(result)
    output_doc = {"schema": SCHEMA, "protocol_id": protocol["protocol_id"], "protocol_sha256": sha,
                  "comparisons": comparisons, "allocated_arms": len(protocol["pairs"]) * len(protocol["conditions"]),
                  "observed_arms": len(arms)}
    write_bytes(output, encode(output_doc) + b"\n", replace=False)
    return output_doc
