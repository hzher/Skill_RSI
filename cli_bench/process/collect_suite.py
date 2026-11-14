"""Run the authorized full-coverage collection sequentially with persistent status."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
ORDER = ["sales_prediction", "cohort_studies", "database_exploration",
         "exploitable_poker", "blind_spectrum_monitoring", "codebase"]


def now():
    return datetime.now().astimezone().isoformat()


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--tasks", nargs="+", choices=ORDER, default=ORDER)
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"order": args.tasks, "concurrent_scene_runs": 1, "executed": False}))
        return
    proof = json.loads((ROOT / "reports/runtime-guard-check.json").read_text(encoding="utf-8"))
    if proof.get("passed") is not True:
        raise RuntimeError("Runtime guard fault tests have not passed")
    directory = ROOT / "reports/collection-suite"
    directory.mkdir(exist_ok=True)
    if (directory / "status.json").is_file() and not (directory / "suite.lock").exists():
        previous = json.loads((directory / "status.json").read_text(encoding="utf-8-sig"))
        history = directory / "previous-suites.json"
        prior = json.loads(history.read_text(encoding="utf-8")) if history.exists() else []
        prior.append(previous)
        save(history, prior)
    lock = directory / "suite.lock"
    with lock.open("x", encoding="utf-8") as output:
        output.write(str(os.getpid()))
    state = {"pid": os.getpid(), "started_at": now(), "status": "running",
             "authorization": "User requested as much complete trajectory coverage as possible",
             "concurrent_scene_runs": 1, "order": args.tasks, "runs": [], "active_task": None,
             "note": "One bounded attempt per config; process completion is audited separately from question scores. No unbounded retries."}
    try:
        for task in args.tasks:
            if (directory / "stop-after-current").exists():
                state["status"] = "stopped_at_requested_boundary"
                break
            config = "collection-" + task + ".json"
            state["active_task"] = task
            record = {"task": task, "config": config, "started_at": now(), "status": "running"}
            state["runs"].append(record)
            save(directory / "status.json", state)
            print(json.dumps({"event": "task_started", "task": task, "time": now()}), flush=True)
            with (directory / (task + ".stdout.log")).open("wb") as stdout, (directory / (task + ".stderr.log")).open("wb") as stderr:
                child = subprocess.Popen([sys.executable, str(ROOT / "scripts/rsi.py"), "run", "--config", config, "--execute"], stdout=stdout, stderr=stderr)
                record["pid"] = child.pid
                save(directory / "status.json", state)
                while child.poll() is None:
                    subprocess.run([sys.executable, str(ROOT / "scripts/collection_progress.py")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                    time.sleep(20)
                record.update({"exit_code": child.returncode, "finished_at": now(), "status": "process_exited"})
            audit = subprocess.run([sys.executable, str(ROOT / "scripts/audit_collection.py")], capture_output=True, text=True, encoding="utf-8", timeout=120)
            (directory / (task + ".audit.log")).write_text(audit.stdout + audit.stderr, encoding="utf-8")
            record["audit_exit_code"] = audit.returncode
            subprocess.run([sys.executable, str(ROOT / "scripts/collection_progress.py")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            save(directory / "status.json", state)
            print(json.dumps({"event": "task_exited", "task": task, "exit_code": child.returncode, "time": now()}), flush=True)
        else:
            state["status"] = "all_configured_attempts_finished_pending_coverage_review"
        state["active_task"] = None
        state["finished_at"] = now()
        save(directory / "status.json", state)
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
