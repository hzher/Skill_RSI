"""Read native live or sealed outcomes without any model calls."""
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
COUNTS = {"codebase_adaptation": 19, "database_exploration": 40, "sales_prediction": 12, "cohort_studies": 20, "blind_spectrum_monitoring": 90, "exploitable_poker": 120}


def runtime_path(value):
    if os.name == "nt":
        return Path("\\\\?\\UNC\\wsl.localhost\\Ubuntu-24.04" + value.replace("/", "\\"))
    return Path(value)


def main():
    status = {"updated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(), "planned_unique_instances": sum(COUNTS.values()), "model_calls_by_monitor": 0, "tasks": {name: {"planned_unique_instances": count, "unique_graded": 0, "graded_trajectories": 0, "passed_trajectories": 0, "phases": {}} for name,count in COUNTS.items()}, "runs": []}
    for run in sorted((ROOT / "runs/collections").glob("*")):
        if not (run / "control/bridge-plan.json").is_file():
            continue
        plan = json.loads((run / "control/bridge-plan.json").read_text(encoding="utf-8-sig"))
        task = plan["effective_config"]["task"]["name"]
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8-sig"))
        native = runtime_path(plan["linux_runtime"])
        files = list((native / "results" / task / "live").glob("*/*.json"))
        if manifest["execution_status"] != "running":
            sealed = list((run / "native/results" / task / "traces").glob("*/*.json"))
            files = sealed or files
        record = {"run_id": run.name, "task": task, "status": manifest["execution_status"], "phases": {}}
        for p in files:
            if p.name == "manifest.json":
                continue
            try:
                j = json.loads(p.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            outcomes = j.get("instance_outcomes") or j.get("execution", {}).get("instance_outcomes") or []
            if not outcomes:
                continue
            if plan.get("resume_from_run"):
                turns = j.get("interactions") or []
                replay_indexes = {x["query"]["instance_index"] for x in turns if (x.get("response", {}).get("metadata") or {}).get("rsi_replayed")}
                new_indexes = {x["query"]["instance_index"] for x in turns if not (x.get("response", {}).get("metadata") or {}).get("rsi_replayed")}
                outcomes = [x for x in outcomes if x["instance_index"] not in replay_indexes - new_indexes]
            phase = j.get("phase", p.stem)
            record["phases"][phase] = {"graded": len(outcomes), "passed": sum(x.get("success") is True for x in outcomes), "instance_ids": [x["instance_id"] for x in outcomes], "reward_sum": sum(x.get("reward",0) or 0 for x in outcomes)}
        status["runs"].append(record)
        graded = sum(v["graded"] for v in record["phases"].values())
        index_path = run / "audit/collection-index.json"
        previous = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {}
        if graded != previous.get("graded_trajectories", 0):
            from audit_collection import audit
            audit(run)
        aggregate = status["tasks"][task]
        for phase, values in record["phases"].items():
            slot = aggregate["phases"].setdefault(phase, {"graded": 0, "passed": 0, "instance_ids": []})
            slot["graded"] += values["graded"]; slot["passed"] += values["passed"]; slot["instance_ids"].extend(values["instance_ids"])
        aggregate["unique_graded"] = len({i for v in aggregate["phases"].values() for i in v["instance_ids"]})
        aggregate["graded_trajectories"] = sum(v["graded"] for v in aggregate["phases"].values())
        aggregate["passed_trajectories"] = sum(v["passed"] for v in aggregate["phases"].values())
    status["unique_graded"] = sum(t["unique_graded"] for t in status["tasks"].values())
    status["graded_trajectories"] = sum(t["graded_trajectories"] for t in status["tasks"].values())
    (ROOT / "reports/coverage-status.json").write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    workflow_path = ROOT / "reports/current-workflow.json"
    workflow = json.loads(workflow_path.read_text(encoding="utf-8-sig")) if workflow_path.exists() else {}
    active = [run for run in status["runs"] if run["status"] == "running"]
    workflow.update({"updated_at": status["updated_at"], "stage": "collecting" if active else "no_active_collection",
                     "active_task": active[-1]["task"] if active else None, "active_run_ids": [run["run_id"] for run in active],
                     "active_tasks": [run["task"] for run in active],
                     "unique_graded": status["unique_graded"], "planned_unique_instances": status["planned_unique_instances"]})
    workflow_path.write_text(json.dumps(workflow, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        from episode_inventory import publish
        publish(status)
        from raw_inventory import publish as publish_raw
        publish_raw()
    except (OSError, ValueError):
        pass  # A viewer/file lock must not interrupt benchmark monitoring.
    print(json.dumps({"unique_graded": status["unique_graded"], "planned_unique": status["planned_unique_instances"], "graded_trajectories": status["graded_trajectories"], "runs": [{k:v for k,v in r.items() if k != "phases"} | {"phases": {p: {k:v for k,v in d.items() if k != "instance_ids"} for p,d in r["phases"].items()}} for r in status["runs"]]}))


if __name__ == "__main__":
    main()
