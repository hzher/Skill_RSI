"""Task-set verifier dispatch beneath NeMo's existing /verify middleware."""
from functools import lru_cache

from integrations.benchmark_tasks import load_task_set
from .config import configured_service


@lru_cache(maxsize=8)
def deployment(task_set_path, verifier_config_path):
    tasks = load_task_set(task_set_path)
    service = configured_service(verifier_config_path)
    for task in tasks["tasks"]:
        service.registry.resolve(task["evaluation"]["backend"])
    return tasks, service


async def evaluate_for_nemo(*, task_set_path, verifier_config_path, task_id, context):
    tasks, service = deployment(task_set_path, verifier_config_path)
    # A namespace-qualified UID is preferred; short IDs must be unambiguous.
    matches = [task for task in tasks["tasks"] if task_id in (task["uid"], task["task_id"])]
    if len(matches) != 1:
        raise ValueError("NeMo task is absent or ambiguous in the configured task set")
    score = await service.evaluate(matches[0], context)
    return {"reward": score["reward"], "mask_sample": score["mask_sample"],
        "evaluation": score, "reward_version": score["verifier_version"],
        "failure_kind": None if score["valid"] else "benchmark:verification_unavailable"}
