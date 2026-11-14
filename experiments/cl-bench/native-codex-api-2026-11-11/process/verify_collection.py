"""Verify coverage and preserved native evidence; never invoke inference."""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import zipfile

from collection_progress import COUNTS
from rsi import filesystem_path
from verify_continuity import verify as verify_continuity

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(filesystem_path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    digest = hashlib.sha256()
    with filesystem_path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="Check every sealed file hash; use after the jobs finish")
    args = parser.parse_args()
    issues = []
    seen = defaultdict(set)
    indexes = defaultdict(set)
    kinds = defaultdict(int)
    checked_hashes = 0
    resource_hashes = 0
    indexed_records = 0
    active = []
    pair_run = None
    paired = {}
    runs = []
    for run in sorted((ROOT / "runs/collections").iterdir()):
        if not (run / "control/bridge-plan.json").exists():
            continue
        plan, manifest = read(run / "control/bridge-plan.json"), read(run / "manifest.json")
        task = plan["effective_config"]["task"]["name"]
        if task == "codebase_adaptation" and plan["include_baseline"]:
            pair_run = run.name
        if manifest["execution_status"] == "running":
            active.append(run.name)
        index = read(run / "audit/collection-index.json") if (run / "audit/collection-index.json").exists() else {"episodes": []}
        phase_ids = defaultdict(set)
        for entry in index["episodes"]:
            episode = read(run / entry["path"])
            indexed_records += 1
            seen[task].add(entry["instance_id"])
            indexes[task].add(entry["instance_index"])
            phase_ids[entry["phase"]].add(entry["instance_id"])
            kind = episode.get("evidence_kind", "unclassified")
            kinds[kind] += 1
            turns = episode["interactions"]
            if kind == "model_interaction":
                if not turns or len(turns) != episode["completed_interactions"]:
                    issues.append("interaction_count:" + entry["path"])
                elif hashlib.sha256(turns[0]["query"]["prompt"].encode()).hexdigest() != episode["visible_initial_prompt_sha256"]:
                    issues.append("public_prompt_hash:" + entry["path"])
                for turn in turns:
                    if not all(key in turn for key in ["query", "response", "observation"]):
                        issues.append("missing_turn_fields:" + entry["path"])
                    elif not isinstance(turn["response"].get("action"), dict) or not isinstance(turn["observation"].get("content"), str):
                        issues.append("missing_action_or_feedback:" + entry["path"])
            elif kind == "environment_auto_resolution":
                actions = (episode.get("environment_evidence") or {}).get("native_hand_history", {}).get("actions", [])
                if task != "exploitable_poker" or turns or not actions or any(a["player"] != "opponent" for a in actions) or actions[-1]["action"] != "FOLD":
                    issues.append("unproven_auto_resolution:" + entry["path"])
            else:
                issues.append("unclassified_evidence:" + entry["path"])
            if episode["native_outcome"]["instance_id"] != entry["instance_id"]:
                issues.append("outcome_identity:" + entry["path"])
            if task == "codebase_adaptation" and manifest["execution_status"] != "running":
                submission = episode.get("full_submission_evidence", {})
                timed_out = episode["native_outcome"].get("metadata", {}).get("timed_out")
                if not timed_out and not submission.get("matching_captures"):
                    issues.append("full_submission_not_joined_to_native_patch:" + run.name + "/" + entry["path"])
            if not filesystem_path(run / episode["source_trace"]).exists() and manifest["execution_status"] != "running":
                issues.append("missing_native_source:" + entry["path"])
        if run.name == pair_run:
            paired = {key: len(value) for key, value in phase_ids.items()}
        params = plan["effective_config"]["system"]["params"]
        if manifest.get("provider") != "rsi_router" or params.get("use_oauth") is not False or params.get("version") != "0.167.0":
            issues.append("execution_identity:" + run.name)
        submissions = run / "control/submission-map.jsonl"
        if submissions.exists():
            for raw in submissions.read_text(encoding="utf-8").splitlines():
                item = json.loads(raw)
                if sha(run / item["path"]) != item["sha256"]:
                    issues.append("submission_hash:" + item["path"])
        if args.full and manifest["execution_status"] != "running":
            if manifest.get("archive_status") != "sealed":
                issues.append("unsealed_native_archive:" + run.name)
            for relative, expected in read(run / "checksums.json").items():
                checked_hashes += 1
                if not filesystem_path(run / relative).is_file() or sha(run / relative) != expected:
                    issues.append("file_checksum:" + run.name + "/" + relative)
            copy_path = run / "control/post-exit-copy.json"
            if not copy_path.exists() or read(copy_path).get("all_match") is not True:
                issues.append("native_copy_not_verified:" + run.name)
            else:
                for copied in read(copy_path)["files"]:
                    target = run / "native" / copied["path"]
                    if not filesystem_path(target).is_file() or sha(target) != copied["sha256"]:
                        issues.append("native_copy_source_hash:" + run.name + "/" + copied["path"])
        runs.append({"run_id": run.name, "task": task, "indexed_records": len(index["episodes"]), "execution_status": manifest["execution_status"]})
    coverage = {task: {"expected": count, "unique_graded": len(seen[task]), "canonical_indexes_complete": indexes[task] == set(range(count))} for task, count in COUNTS.items()}
    coverage_complete = all(v["unique_graded"] == v["expected"] and v["canonical_indexes_complete"] for v in coverage.values())
    pair_complete = paired.get("baseline") == 19 and paired.get("run", paired.get("rollout", 0)) == 19
    continuity = verify_continuity(pair_run) if pair_run else None
    if args.full:
        source_lock = read(ROOT / "config/sources.lock.json")["sources"]
        for source in source_lock.values():
            for relative, expected in source.get("files_sha256", {}).items():
                if sha(ROOT / source["path"] / relative) != expected:
                    issues.append("pinned_source_changed:" + source["path"] + "/" + relative)
        resources = read(ROOT / "reports/task-resources/resources.json")
        resource_source = ROOT / source_lock["benchmark_isolation_candidate_lf"]["path"]
        for task in resources["tasks"].values():
            for entry in task.get("data_files", []):
                if entry.get("sha256"):
                    resource_hashes += 1
                    if sha(resource_source / entry["path"]) != entry["sha256"]:
                        issues.append("resource_hash:" + entry["path"])
        for run in (ROOT / "runs/smoke").iterdir():
            if not (run / "checksums.json").exists():
                continue
            for relative, expected in read(run / "checksums.json").items():
                checked_hashes += 1
                if not filesystem_path(run / relative).is_file() or sha(run / relative) != expected:
                    issues.append("validation_smoke_checksum:" + run.name + "/" + relative)
        for archive in (ROOT / "archives").glob("process-history-*.zip"):
            with zipfile.ZipFile(archive) as saved:
                for entry in json.loads(saved.read("archive-index.json"))["files"]:
                    if hashlib.sha256(saved.read(entry["path"])).hexdigest() != entry["sha256"]:
                        issues.append("historical_archive_checksum:" + entry["path"])
    report = {"updated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(), "benchmark": "CL-Bench",
              "default_schedules_covered": coverage_complete, "codebase_pair_complete": pair_complete,
              "all_collection_complete": coverage_complete and pair_complete and not active and not issues and bool(continuity and continuity["full_pair_verification_complete"]),
              "full_hash_verification_performed": args.full, "sealed_file_hashes_checked": checked_hashes,
              "frozen_resource_hashes_checked": resource_hashes,
              "unique_graded_instances": sum(len(v) for v in seen.values()), "indexed_records": indexed_records,
              "evidence_kind_counts": dict(kinds), "coverage": coverage, "codebase_pair_run": pair_run,
              "codebase_phase_counts": paired, "active_runs": active, "issues": issues, "runs": runs,
              "model_calls_by_verifier": 0, "score_note": "Completed grading includes native model failures; completion does not mean all answers passed."}
    (ROOT / "reports/collection-completion.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["all_collection_complete", "unique_graded_instances", "indexed_records", "codebase_phase_counts", "active_runs", "sealed_file_hashes_checked", "issues"]}))
    if args.full and not report["all_collection_complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
