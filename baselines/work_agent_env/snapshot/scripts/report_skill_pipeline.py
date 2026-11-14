"""Summarize distinct real runs and deterministic response recovery without mixing identities."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.benchmark_tasks.store import save
from integrations.experience import ExperienceStore, curator_view
from integrations.skill_library import SkillLibrary
from scripts.recover_skill_responses import merge_edits


def read(path):
    return json.loads(Path(path).read_text())


def sum_usage(rows):
    return {field: sum(row.get(field, 0) or 0 for row in rows) for field in
            ("prompt_tokens", "completion_tokens", "total_tokens")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--revalidated", type=Path, required=True)
    parser.add_argument("--recovery", type=Path, required=True)
    parser.add_argument("--generator-retry", type=Path)
    parser.add_argument("--extra-generator-attempt", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    primary, recovery = read(args.primary / "receipt.json"), read(args.recovery / "receipt.json")
    revalidated = read(args.revalidated / "receipt.json")
    assert primary["stage"] == recovery["stage"] == "finished"
    main_execution = read(primary["execution_receipt"])
    recovery_execution = read(recovery["execution_receipt"])
    task_uids = [task["uid"] for task in read(args.primary / "tasks.json")["tasks"]]
    primary_rows = {row["task_uid"]: row for row in main_execution["results"]}
    selected, attempts = primary_rows.copy(), []
    for row in recovery_execution["results"]:
        assert row["task_uid"] in selected
        previous = selected[row["task_uid"]]
        assert previous["model_requests_attempted"] == 0 and previous["failure_stage"] == "initializing"
        selected[row["task_uid"]] = row
    store, library = ExperienceStore(ROOT / ".runtime/experience"), SkillLibrary(ROOT / ".runtime/skill_library")
    validated_by_uid = {row["task_uid"]: row for row in revalidated["results"] if row["status"] == "validated"}
    recovery_gen = {row["task_uid"]: row for row in recovery["generation"]}
    retry = read(args.generator_retry / "receipt.json") if args.generator_retry else None
    extra_attempts = [read(path / "receipt.json") for path in args.extra_generator_attempt]
    if retry is not None and retry["status"] in ("generated", "no_change"):
        recovery_gen[retry["task_uid"]] = retry
    tasks = []
    task_usage = []
    for source, execution in [("primary", main_execution), ("targeted_recovery", recovery_execution)]:
        for row in execution["results"]:
            attempts.append({"source": source, "run_id": execution["run_id"], "task_uid": row["task_uid"],
                "status": row["status"], "failure_stage": row.get("failure_stage"), "error": row.get("error_details"),
                "model_requests_attempted": row["model_requests_attempted"], "resources_closed": row["resources_closed"]})
            record = store.get(row["experience"]["id"])
            task_usage.extend(record["costs"]["usage_by_response"])
    for uid in task_uids:
        row = selected[uid]
        record = store.get(row["experience"]["id"])
        result = curator_view(record)["result"]
        score = row.get("evaluation") or {}
        raw = score.get("raw") or {}
        candidate = recovery_gen.get(uid, validated_by_uid.get(uid))
        generated = candidate is not None and candidate["status"] in ("validated", "generated", "no_change")
        tasks.append({"task_uid": uid, "execution_status": row["status"], "original_score": result,
            "reviewed_passed": (raw.get("reviewed_evaluation") or {}).get("pass"),
            "effective_passed": score.get("passed"), "experience_id": row["experience"]["id"],
            "generator_candidate_valid": generated, "candidate_id": candidate.get("candidate_id") if generated else None,
            "skill_version": candidate.get("version") if generated else None,
            "generator_status": candidate.get("status") if candidate else None})
    generation_usage, response_ids = [], []
    for directory in (args.primary, args.recovery):
        for path in sorted((directory / "generator").glob("*/provider_reply.json")):
            reply = read(path)
            generation_usage.append(reply.get("usage") or {})
            response_ids.append(reply.get("provider_response_id"))
    if args.generator_retry and (args.generator_retry / "provider_reply.json").exists():
        reply = read(args.generator_retry / "provider_reply.json")
        generation_usage.append(reply.get("usage") or {})
        response_ids.append(reply.get("provider_response_id"))
    for directory in args.extra_generator_attempt:
        if (directory / "provider_reply.json").exists():
            reply = read(directory / "provider_reply.json")
            generation_usage.append(reply.get("usage") or {})
            response_ids.append(reply.get("provider_response_id"))
    assert all(response_ids) and len(set(response_ids)) == len(response_ids), "Provider response identities are missing or reused"
    pool = library.get(revalidated["diagnostic_pool_version"])
    recovery_version = retry["version"] if retry and retry["status"] in ("generated", "no_change") else recovery["final_skill_version"]
    pool, conflicts = merge_edits(pool, {}, library.get(recovery_version))
    version = library.snapshot(pool)
    inventory = []
    for name, content in sorted(pool.items()):
        file = "skills/" + hashlib.sha256(name.encode()).hexdigest()[:16] + ".md"
        target = args.output / file
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as stream:
            stream.write(content)
        inventory.append({"name": name, "file": file, "sha256": hashlib.sha256(content.encode()).hexdigest()})
    save(args.output / "skill_inventory.json", inventory)
    summary = {"unique_tasks": len(tasks), "execution_attempts": len(attempts),
        "primary_valid_original_scores": primary["summary"]["valid_original_scores"],
        "primary_original_passed": primary["summary"]["original_passed"],
        "primary_initialization_failures": sum(row.get("failure_stage") == "initializing" for row in main_execution["results"]),
        "valid_original_scores_after_recovery": sum(row["original_score"]["status"] == "measured" for row in tasks),
        "original_passed_after_recovery": sum(row["original_score"]["passed"] is True for row in tasks),
        "reviewed_additional_passed": sum(row["original_score"]["passed"] is False and row["reviewed_passed"] is True for row in tasks),
        "effective_passed_after_recovery": sum(row["effective_passed"] is True for row in tasks),
        "valid_skill_candidates": sum(row["generator_candidate_valid"] for row in tasks),
        "real_generator_responses": len(response_ids),
        "generator_diagnostic_retry_requests": (retry["logical_requests"] if retry else 0) + sum(row["logical_requests"] for row in extra_attempts),
        "generator_logical_requests": sum("request_id" in row for row in primary["generation"] + recovery["generation"]) +
            (retry["logical_requests"] if retry else 0) + sum(row["logical_requests"] for row in extra_attempts),
        "generator_api_error_attempts": sum(row.get("error", {}).get("status_code") is not None
            for row in primary["generation"] + recovery["generation"] + extra_attempts + ([retry] if retry else [])),
        "namespace_adapted_responses": revalidated["namespace_adapted"] +
            sum(bool(read(path)["provenance"].get("transport", {}).get("aliases")) for path in (args.recovery / "generator").glob("*/candidate.json")) +
            (int(bool(read(args.generator_retry / "candidate.json")["provenance"].get("transport", {}).get("aliases")))
             if args.generator_retry and (args.generator_retry / "candidate.json").exists() else 0),
        "diagnostic_pool_skills": len(pool), "merge_conflicts": revalidated["merge_conflicts"] + len(conflicts),
        "all_resources_closed": all(row["resources_closed"] is True for row in attempts)}
    receipt = {"schema_version": "workagent-real-skill-pipeline-summary-v1", "observed_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary, "tasks": tasks, "attempts": attempts,
        "primary_receipt": str(args.primary / "receipt.json"), "response_revalidation_receipt": str(args.revalidated / "receipt.json"),
        "targeted_recovery_receipt": str(args.recovery / "receipt.json"),
        "generator_retry_receipt": str(args.generator_retry / "receipt.json") if args.generator_retry else None,
        "extra_generator_attempts": [str(path / "receipt.json") for path in args.extra_generator_attempt],
        "diagnostic_pool_version": version, "diagnostic_pool_is_generator_sample": False,
        "diagnostic_pool_published_to_main": False, "recovery_merge_conflicts": conflicts,
        "source_versions": {"primary": primary["source_sha256"], "targeted_recovery": recovery["source_sha256"],
                            "response_revalidation": revalidated["source_sha256"], "generator_retry": retry["source_sha256"] if retry else None},
        "model": primary["options"]["model"], "generator_model": primary["options"]["generator_model"],
        "package_sha256": primary["package_sha256"], "generator_provider_response_ids": response_ids,
        "costs": {"task_api_usage": sum_usage(task_usage), "generator_api_usage": sum_usage(generation_usage),
                  "money": None, "money_status": "not_reported", "revalidation_model_requests": 0},
        "training_admitted": False, "generator_token_logprob_capture_verified": False,
        "downstream_skill_effectiveness_verified": False, "verl_update_verified": False,
        "report_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    save(args.output / "receipt.json", receipt)
    print(json.dumps({"output": str(args.output), **summary, "costs": receipt["costs"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
