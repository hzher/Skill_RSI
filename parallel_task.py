"""Run priority tasks concurrently under one batch owner, then continue the suite."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import fcntl
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace

import run

PRIORITY_WORKERS = {
    "blind_spectrum_monitoring": 2,
    "database_exploration": 2,
    "codebase_adaptation": 1,
    "sales_prediction": 1,
}


def continue_with_priority(batch: Path, config_path: Path, priority: tuple[str, ...]) -> None:
    manifest_path = batch / "manifest.json"
    print("Waiting for exclusive ownership of the batch", flush=True)
    with (batch / ".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        manifest = run.load_json(manifest_path)
        if manifest["profile"] != "full" or manifest["skill_loading"]["sha256"]:
            raise ValueError("Priority scheduling requires an existing full batch without a loaded Skill")
        config = run.load_config(config_path)
        runtime, resources, sources = run.runtime_provenance(config)
        loading = manifest["skill_loading"]
        comparison = run.canonical_hash({
            "profile": "full", "system": config["system"], "pricing_provenance": config.get("pricing_provenance"),
            "sources": sources, "runtime": runtime, "resources": resources, "capture_schema_version": 1,
        })
        execution = run.canonical_hash({"comparison_fingerprint": comparison, "skill_sha256": loading["sha256"]})
        accepted = (manifest["fingerprint"], *manifest.get("execution_revisions", {}))
        if execution not in accepted:
            raise ValueError("Priority scheduler executor differs from the batch's registered revisions")
        if any(entry.get("status") == "running" for entry in manifest["tasks"].values()):
            raise ValueError("Previous scheduler exited without recording a handoff boundary")
        config["skill_loading"] = loading
        run.use_benchmark_data()
        provenance = {"fingerprint": manifest["fingerprint"], "execution_fingerprint": execution,
                      "accepted_executions": accepted}
        poker = "exploitable_poker"
        if manifest["tasks"].get(poker, {}).get("status") != "completed":
            manifest["tasks"][poker] = {"status": "running"}
            run.save_json(manifest_path, manifest)
            try:
                details = run.run_task(poker, batch, "full", config, 4, provenance)
            except BaseException as exc:
                manifest["tasks"][poker] = {"status": "failed", "error": str(exc)}
                run.save_json(manifest_path, manifest)
                raise
            manifest["tasks"][poker] = {"status": "completed", **details}
            run.save_json(manifest_path, manifest)
        selected = [task for task in priority if manifest["tasks"].get(task, {}).get("status") != "completed"]
        workers = {task: PRIORITY_WORKERS[task] for task in selected}
        record = {"policy": "parallel-priority-disjoint-tasks", "tasks": selected, "workers_per_task": workers,
                  "source_sha256": run.sha256(Path(__file__)), "started_at": run.now(), "status": "running"}
        manifest.setdefault("scheduler_history", []).append(record)
        for task in selected:
            manifest["tasks"][task] = {"status": "running"}
        run.save_json(manifest_path, manifest)
        failures = []
        if selected:
            with ProcessPoolExecutor(max_workers=len(selected), mp_context=multiprocessing.get_context("spawn")) as pool:
                futures = {
                    pool.submit(run.run_task, task, batch, "full", config, workers[task], provenance): task
                    for task in selected
                }
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        details = future.result()
                    except Exception as exc:
                        failures.append(task)
                        manifest["tasks"][task] = {"status": "failed", "error": str(exc)}
                        print(f"Priority task failed: {task}: {type(exc).__name__}", flush=True)
                    else:
                        manifest["tasks"][task] = {"status": "completed", **details}
                    run.save_json(manifest_path, manifest)
        record.update(status="failed" if failures else "completed", ended_at=run.now())
        run.save_json(manifest_path, manifest)
        if failures:
            raise RuntimeError(f"Unfinished priority tasks: {', '.join(failures)}")
        pending = [task for task in run.TASKS if manifest["tasks"].get(task, {}).get("status") != "completed"]
        args = SimpleNamespace(command="full", resume_repair=False, skill=None, workers=4, docker_workers=2)
        run.run_batch(args, config, batch, pending)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tasks", choices=tuple(PRIORITY_WORKERS), nargs="+", required=True)
    args = parser.parse_args()
    batch, config = args.batch.resolve(strict=True), args.config.resolve(strict=True)
    if batch.parent != run.ROOT / "results":
        parser.error("Select a batch under this project's results directory")
    os.chdir(run.ROOT)
    continue_with_priority(batch, config, tuple(dict.fromkeys(args.tasks)))


if __name__ == "__main__":
    main()
