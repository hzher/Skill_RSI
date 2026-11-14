"""Evaluate completed task environments with registered original Bench adapters."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.benchmark_tasks import load_task_set, strict_json
from integrations.verifiers.config import configured_service


async def evaluate(task_set, service, contexts):
    # Reject unsupported bindings/missing contexts before running the first evaluator.
    for task in task_set["tasks"]:
        service.registry.resolve(task["evaluation"]["backend"])
        if task["uid"] not in contexts or not isinstance(contexts[task["uid"]], dict):
            raise ValueError("Missing task evaluation context: " + task["uid"])
    return [await service.evaluate(task, contexts[task["uid"]]) for task in task_set["tasks"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("task-set", "verifiers", "contexts", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    tasks = load_task_set(args.task_set)
    contexts = strict_json(args.contexts.read_text())
    scores = asyncio.run(evaluate(tasks, configured_service(args.verifiers), contexts))
    result = {"schema_version": "workagent-evaluation-v1", "tasks_sha256": tasks["tasks_sha256"],
        "scores": scores, "training_admitted": False}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"tasks": len(scores), "valid": sum(s["valid"] for s in scores), "output": str(args.output)}))
    return 0 if all(s["valid"] for s in scores) else 2


if __name__ == "__main__":
    raise SystemExit(main())
