"""Audit already archived native evidence in WSL; optionally regrade, no model calls."""
from __future__ import annotations

import argparse
import csv
import dataclasses
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--regrade", action="store_true")
    args = parser.parse_args()
    if Path(args.run_id).name != args.run_id:
        raise ValueError("Use one run ID, not a path")
    run = ROOT / "runs/smoke" / args.run_id
    manifest = load(run / "manifest.json")
    plan = load(run / "control/bridge-plan.json")
    source = Path(plan["linux_source"])
    isolated = plan.get("actor_isolation") == "pr26_sanitized"
    current_sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if current_sha != manifest["benchmark_sha"]:
        raise RuntimeError("Pinned source mismatch; cannot audit/regrade with a different version")
    traces = list((run / "native").glob("results/codebase_adaptation/traces/*/run_0000.json"))
    if len(traces) != 1:
        raise RuntimeError("Expected exactly one native rollout trace")
    trace_path = traces[0]
    trace = load(trace_path)
    outcomes = trace["instance_outcomes"]
    if len(outcomes) != 1 or outcomes[0]["instance_id"] != manifest["episode_id"]:
        raise RuntimeError("Native instance identity/count mismatch")
    outcome = outcomes[0]
    entries = trace["result"]["metrics"]["interaction_trace"]
    submissions = [e for e in entries if e.get("is_submit")]
    if len(submissions) != 1 or len(trace["interactions"]) != len(entries):
        raise RuntimeError("Tool/observation or submission count mismatch")
    submission = submissions[0]
    native_verifier = submission["eval_result"]
    patch = submission.get("model_patch", "")
    captured_map = run / "control/submission-map.jsonl"
    if captured_map.is_file():
        captures = [json.loads(line) for line in captured_map.read_text().splitlines() if json.loads(line)["instance_id"] == manifest["episode_id"]]
        if len(captures) == 1:
            complete = run / captures[0]["path"]
            if sha(complete) != captures[0]["sha256"]:
                raise RuntimeError("Full submitted patch checksum mismatch")
            patch = complete.read_text(encoding="utf-8")
    if not patch or (len(patch) >= 5000 and not captured_map.is_file()):
        raise RuntimeError("The native trace patch is missing or may have been truncated")
    audit = run / "audit"
    audit.mkdir(exist_ok=True)
    (audit / "submitted-worktree.patch").write_text(patch, encoding="utf-8")
    write(audit / "public-input.json", trace["interactions"][0]["query"])
    with (audit / "tool-calls.jsonl").open("w", encoding="utf-8") as output:
        for event, native in zip(trace["interactions"], entries):
            record = {"step": event["step_number"], "timestamp": event["timestamp"], "command": native["command"], "returncode": native.get("returncode"), "output": native.get("output"), "observation": event["observation"], "submitted": native.get("is_submit", False), "source_trace": str(trace_path.relative_to(run))}
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
    artifacts = trace_path.parent / "artifacts/run_0000"
    artifact_manifest = load(artifacts / "manifest.json")
    shutil.copy2(artifacts / "events.jsonl", audit / "events.jsonl")
    sessions = artifact_manifest["codex_conversation_jsonl_paths"]
    if not sessions:
        raise RuntimeError("No native Codex session was exported")
    session_metadata = []
    for i, relative in enumerate(sessions, 1):
        original = artifacts / relative
        compact = audit / f"session-{i:03d}.jsonl"
        shutil.copy2(original, compact)
        if sha(original) != sha(compact):
            raise RuntimeError("Session copy checksum mismatch")
        meta = json.loads(compact.read_text().splitlines()[0])["payload"]
        if meta.get("model_provider") != "rsi_router":
            raise RuntimeError("Native session does not identify the team router provider")
        session_metadata.append({k: meta.get(k) for k in ("id", "originator", "cli_version", "source", "model_provider")})
    events = [json.loads(line) for line in (audit / "events.jsonl").read_text().splitlines() if line.strip()]
    completed = [e for e in events if e.get("type") == "turn.completed"]
    agent_messages = [e["item"]["text"] for e in events if e.get("type") == "item.completed" and e.get("item", {}).get("type") == "agent_message"]
    if len(completed) < len(entries) or not agent_messages:
        raise RuntimeError("Native completed turns are insufficient for task steps")
    (audit / "final.txt").write_text(agent_messages[-1], encoding="utf-8")
    if not outcome["success"] or native_verifier != {"success": True, "status": "completed"}:
        raise RuntimeError("This acceptance routine requires an actual native passing result")
    dataset = source / "data/codebase_adaptation/final-dataset.jsonl"
    instance = next(json.loads(line) for line in dataset.read_text().splitlines() if json.loads(line)["instance_id"] == manifest["episode_id"])
    sys.path.insert(0, str(source))
    from src.tasks.codebase_adaptation.generic_runtime import strip_patch_paths, test_patch_paths
    graded_patch = strip_patch_paths(patch, test_patch_paths(instance))
    (audit / "graded-code.patch").write_text(graded_patch, encoding="utf-8")
    if args.regrade:
        if (audit / "verifier-replay.json").exists():
            raise RuntimeError("Existing replay evidence is immutable; use another audit revision to replay again")
        from src.tasks.codebase_adaptation.evaluator import evaluate_submission
        result = evaluate_submission(model_patch=patch, instance=instance)
        replay = dataclasses.asdict(result)
        write(audit / "verifier-replay.json", {"benchmark_sha": current_sha, "instance_id": manifest["episode_id"], "submitted_patch_sha256": sha(audit / "submitted-worktree.patch"), "result": replay, "model_calls": 0})
        if not result.success:
            raise RuntimeError("Archived patch did not pass independent regrading")
    replay_exists = (audit / "verifier-replay.json").is_file()
    if replay_exists and not load(audit / "verifier-replay.json")["result"]["success"]:
        raise RuntimeError("Recorded independent replay failed")
    write(audit / "verifier.json", {"native_result": native_verifier, "instance_outcome": outcome, "native_score": trace["result"]["score"], "source_trace": str(trace_path.relative_to(run)), "source_json_pointer": f"/result/metrics/interaction_trace/{len(entries)-1}/eval_result", "replayed_from_archived_patch": replay_exists})
    usage = trace["execution"]["usage"]["respond"]
    write(audit / "usage.json", {"native_framework_usage": usage, "last_cumulative_cli_usage": completed[-1]["usage"], "observed_completed_turns": len(completed), "framework_cost_is_estimate": True, "actual_router_bill": None})
    boundaries = [json.loads(line) for line in (run / "control/actor-boundaries.jsonl").read_text().splitlines()] if isolated else []
    if isolated and (not boundaries or not all(b["base_tree_matches"] and b["git_commit_count"] == 1 and b["inherited_base_object_inaccessible"] for b in boundaries)):
        raise RuntimeError("Missing passing actor-boundary evidence")
    protocol = {"source": "src/tasks/codebase_adaptation/sanitized_runtime.py" if isolated else "src/tasks/codebase_adaptation/generic_runtime.py:initialize_generic_pr_container", "native_test_patch_visible_in_task_workspace": not isolated, "reference_solution_patch_injected_by_this_runner": False, "evaluation_in_fresh_container": True, "grader_restores_test_owned_paths": True, "strict_hidden_test_requirement_alignment": "verified_candidate" if isolated else "pending_team_confirmation", "formal_statistics_eligible": False, "source_publication_status": "unmerged_PR26_including_PR25" if isolated else "published_main", "actor_boundary_evidence": boundaries}
    write(audit / "protocol.json", protocol)
    stamp = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    manifest.update({"execution_status": "completed_pass", "grading_status": "completed", "native_score": trace["result"]["score"], "native_reward": outcome["reward"], "native_steps": outcome["metadata"]["steps"], "session_ids": [s["id"] for s in session_metadata], "initial_state": {"repo": instance["repo"], "base_commit": instance["base_commit"], "image": instance["image_name"]}, "input_sha256": sha(audit / "public-input.json"), "input_sha256_definition": "SHA-256 of audit/public-input.json, exact saved public Query serialization", "usage": {"model_calls": usage["call_count"], "total_tokens": usage["total_tokens"], "framework_estimated_cost_usd": usage["cost_usd"], "actual_router_billed_cost": None}, "included_in_statistics": False, "archive_status": "sealed", "audited_at": stamp, "protocol_alignment": "verified_isolated_candidate" if isolated else "pending_test_visibility_confirmation", "missing_evidence": ["Router's independent upstream model identity and actual billed cost are not available; original native evaluation retains only success/status, with full independent replay result saved separately."]})
    manifest["evidence"].update({"verifier": "audit/verifier.json", "verifier_replay": "audit/verifier-replay.json" if replay_exists else None, "tools": "audit/tool-calls.jsonl", "submitted_patch": "audit/submitted-worktree.patch", "graded_code": "audit/graded-code.patch", "sessions": [f"audit/session-{i:03d}.jsonl" for i in range(1,len(sessions)+1)], "protocol": "audit/protocol.json"})
    prior = sorted(p.name for p in run.parent.iterdir() if p.is_dir() and p.name < run.name)
    if prior:
        manifest["previous_attempt_id"] = prior[-1] + "/attempt_001"
    write(run / "manifest.json", manifest)
    header = (ROOT / "templates/index.csv").read_text().strip().split(",")
    with (run / "index.csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=header); writer.writeheader()
        writer.writerow({"run_id": run.name, "phase": "smoke", "series_id": run.name, "episode_id": manifest["episode_id"], "attempt_id": "attempt_001", "process_exit_code": manifest["process_exit_code"], "execution_status": "completed_pass", "grading_status": "completed", "native_score": manifest["native_score"], "native_reward": manifest["native_reward"], "native_steps": manifest["native_steps"], "included_in_statistics": "false", "manifest_path": "manifest.json", "native_trace_path": str(trace_path.relative_to(run)), "artifact_path": "audit/submitted-worktree.patch", "notes": "Smoke only; original test_patch is visible per upstream initialization; formal protocol alignment pending"})
    acceptance = {"accepted": isolated and replay_exists, "native_wiring_passed": True, "native_verifier_passed": True, "archived_patch_regrade_passed": replay_exists, "run_id": run.name, "benchmark_sha": current_sha, "actor_isolation": isolated, "reviewed_at": stamp, "native_verifier_evidence": "audit/verifier.json", "tool_return_evidence": "audit/tool-calls.jsonl", "artifact_evidence": "audit/submitted-worktree.patch", "notes": "Verified isolated candidate smoke for user-authorized trajectory collection; unmerged source is recorded explicitly." if isolated else "Technical chain passes. Formal acceptance is pending alignment of upstream visible test_patch with the guide's hidden-test isolation requirement."}
    write(ROOT / "reports/smoke-acceptance.json", acceptance)
    summary = {"run_id": run.name, "episode_id": manifest["episode_id"], "native_passed": True, "native_reward": manifest["native_reward"], "native_steps": manifest["native_steps"], "native_model_calls": usage["call_count"], "native_tokens": usage["total_tokens"], "framework_estimated_cost_usd": usage["cost_usd"], "archived_patch_regrade_passed": replay_exists, "native_cli_version": session_metadata[0]["cli_version"], "native_provider": session_metadata[0]["model_provider"], "session_ids": manifest["session_ids"], "archive": str(run.relative_to(ROOT)), "formal_batch_started": False, "formal_statistics_eligible": False, "protocol_alignment": "verified_isolated_candidate" if isolated else "pending_test_visibility_confirmation", "completed_at": stamp}
    write(ROOT / "reports/smoke-summary.json", summary)
    write(ROOT / "reports/current-workflow.json", {**summary, "stage": "smoke_completed_formal_protocol_pending"})
    # All additions and annotations are now final before checksumming.
    checksums = {f.relative_to(run).as_posix(): sha(f) for f in run.rglob("*") if f.is_file() and not f.is_symlink() and f.name != "checksums.json"}
    write(run / "checksums.json", checksums)
    if any(sha(run / relative) != expected for relative,expected in checksums.items()):
        raise RuntimeError("Final archive checksum verification failed")
    summary["checksummed_files"] = len(checksums)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
