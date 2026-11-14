"""Attribute measured downstream evidence to exact candidate and frozen Executor."""
from integrations.benchmark_tasks.contracts import digest, nonempty, validate_task
from integrations.experience import curator_view


def attribute(candidate, execution, *, fixed_executor_id):
    nonempty(fixed_executor_id, "fixed Executor identity")
    body = {k: v for k, v in candidate.items() if k != "id"}
    if digest(body) != candidate["id"]:
        raise ValueError("Candidate changed")
    provenance = candidate["provenance"]
    task = validate_task(execution["task"])
    receipt = execution["receipt"]
    if receipt.get("task_uid") != task["uid"] or receipt.get("task_sha256") != digest(task):
        raise ValueError("Downstream task differs from its execution receipt")
    score_receipt = receipt.get("evaluation") or {}
    if score_receipt.get("verifier_input_sha256", digest(task)) != digest(task):
        raise ValueError("Downstream evaluator input identity differs")
    if execution["task"]["uid"] == provenance["seed_task_uid"]:
        raise ValueError("Seed task cannot stand in for independent downstream execution")
    executor = execution["executor"]
    if executor.get("fixed_executor_id") != fixed_executor_id or executor.get("skill_version") != candidate["version"]:
        raise ValueError("Downstream Executor/skill identity differs")
    score = curator_view(execution)["result"]
    return {"schema_version": "workagent-skill-attribution-v1", "candidate_id": candidate["id"],
            "skill_version": candidate["version"], "generator_policy_id": provenance["generator_policy_id"],
            "fixed_executor_id": fixed_executor_id, "downstream_experience_id": digest(execution),
            "task_uid": execution["task"]["uid"], "reward": score["reward"],
            "mask_sample": score["status"] != "measured", "score_basis": score["score_basis"],
            "verifier_version": score["verifier_version"], "training_admitted": False}
