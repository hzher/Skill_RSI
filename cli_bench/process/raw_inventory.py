"""Locate raw, partial, validation and historical evidence without inventing scores."""
import json
import os
from pathlib import Path
import zipfile
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def publish():
    rows = []
    continued = {}
    for path in (ROOT / "runs/collections").glob("*/control/bridge-plan.json"):
        plan = read(path)
        if plan.get("resume_from_run"):
            continued.setdefault(plan["resume_from_run"], []).append(path.parent.parent.name)
    for scope in ["collections", "smoke"]:
        for run in sorted((ROOT / "runs" / scope).iterdir()):
            if not (run / "manifest.json").exists():
                continue
            manifest = read(run / "manifest.json")
            index_path = run / "audit/collection-index.json"
            index = read(index_path) if index_path.exists() else {}
            plan_path = run / "control/bridge-plan.json"
            plan = read(plan_path) if plan_path.exists() else {}
            known = ["manifest.json", "checksums.json", "stdout.log", "stderr.log", "control/bridge-plan.json",
                     "control/visible-events.jsonl", "control/native-calls", "control/submission-map.jsonl",
                     "control/submissions", "control/actor-boundaries.jsonl", "control/resume-evidence.json",
                     "recovery-before-restart", "native", "audit/collection-index.json", "audit/episodes"]
            rows.append({"run_id": run.name, "classification": "benchmark_collection" if scope == "collections" else "validation_smoke_excluded_from_coverage",
                         "execution_status": manifest["execution_status"], "archive_status": manifest.get("archive_status"),
                         "available_paths": [(run / name).relative_to(ROOT).as_posix() for name in known if (run / name).exists()],
                         "scored_episode_records": index.get("graded_trajectories"),
                         "ungraded_submission_instance_ids": index.get("ungraded_submission_instance_ids", []),
                         "continued_by_runs": continued.get(run.name, []), "resumes_run": plan.get("resume_from_run"),
                         "live_linux_runtime": plan.get("linux_runtime") if manifest["execution_status"] == "running" else None,
                         "raw_evidence_scope": "original native exports and CLI input/output; derived episodes are an index, not a replacement"})
    history = []
    for archive in (ROOT / "archives").glob("process-history-*.zip"):
        with zipfile.ZipFile(archive) as saved:
            names = saved.namelist()
            for name in names:
                parts = name.split("/")
                if len(parts) != 5 or parts[:2] != ["rsi-workspace", "runs"] or parts[-1] != "manifest.json":
                    continue
                manifest = json.loads(saved.read(name).decode("utf-8-sig"))
                prefix = "/".join(parts[:-1]) + "/"
                history.append({"archive": archive.relative_to(ROOT).as_posix(), "run_id": parts[3],
                                "classification": "historical_failed_attempt_excluded_from_scored_coverage",
                                "error_type": manifest.get("error_type"), "member_prefix": prefix,
                                "member_paths": [n for n in names if n.startswith(prefix)]})
    report = {"updated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(), "model_calls_by_inventory": 0,
              "runs": rows, "historical_failed_attempts": history,
              "rules": ["Keep original evidence, partial attempts and validation samples distinct from scored coverage.",
                        "Do not fabricate missing observations, scores or hidden model internals.",
                        "Native benchmark truncation remains visible in original records; a derived index cannot recover unavailable upstream text.",
                        "Authentication files and personal sessions are not trajectory evidence and are excluded."]}
    path = ROOT / "reports/raw-evidence-inventory.json"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"indexed_runs": len(rows), "historical_failed_attempts": len(history)}


if __name__ == "__main__":
    print(json.dumps(publish()))
