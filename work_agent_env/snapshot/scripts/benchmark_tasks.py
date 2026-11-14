"""Register, test and execute any standard package installed under benchmarks/."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.benchmark_tasks import load_task_set, make_task_set, strict_json
from integrations.benchmark_tasks.store import TaskStore, save
from integrations.executors import ExecutorOptions, ExecutorService, default_executor_registry
from integrations.executors.api import APIExecutor


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for name in ("register", "test", "run"):
        command = sub.add_parser(name)
        command.add_argument("--benchmark", required=name != "run")
        command.add_argument("--task", action="append")
        command.add_argument("--output", type=Path)
        if name == "test":
            command.add_argument("--runtime", action="store_true")
        if name == "run":
            command.add_argument("--task-set", type=Path)
            command.add_argument("--provider", choices=("openai", "anthropic"), required=True)
            command.add_argument("--model", required=True)
            command.add_argument("--skill-version", help="Immutable Markdown skill snapshot SHA256")
            command.add_argument("--concurrency", type=int, default=1)
            command.add_argument("--max-turns", type=int, default=50)
            command.add_argument("--max-output-tokens", type=int, default=4096)
            command.add_argument("--task-timeout", type=float, default=1800)
            command.add_argument("--request-timeout", type=float, default=180)
            command.add_argument("--run-id")
            command.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    store = TaskStore(ROOT)
    try:
        if args.command == "list":
            print(json.dumps({"benchmarks": store.discover()}))
            return 0
        if args.command == "run" and bool(args.benchmark) == bool(args.task_set):
            raise ValueError("Choose exactly one of --benchmark or --task-set")
        registration = store.register(args.benchmark) if args.benchmark else None
        tasks = load_task_set(registration["task_set"] if registration else args.task_set)
        if args.task:
            selected = set(args.task)
            if len(selected) != len(args.task) or not selected <= {task["task_id"] for task in tasks["tasks"]}:
                raise ValueError("Unknown or duplicate task selection")
            tasks = make_task_set(task for task in tasks["tasks"] if task["task_id"] in selected)
            store.put(tasks)
        if args.command == "register":
            result = registration
        elif args.command == "test":
            result = {"schema_version": "workagent-package-check-v1", "benchmark": args.benchmark,
                "tasks": len(tasks["tasks"]), "tasks_sha256": tasks["tasks_sha256"], "static_valid": True,
                "runtime_tested": args.runtime, "model_calls": 0, "benchmark_scores_produced": False,
                "results": []}
            if args.runtime:
                async def runtime():
                    executor = APIExecutor(project_root=ROOT)
                    directory = ROOT / ".runtime/package-tests" / uuid.uuid4().hex
                    directory.mkdir(parents=True)
                    rows = []
                    package_cache = {}
                    for task in tasks["tasks"]:
                        package = store.package_for(task, cache=package_cache)
                        smoke_path = package / "smoke.json"
                        smoke = strict_json(smoke_path.read_text()).get(task["task_id"], {}) if smoke_path.exists() else {}
                        rows.append(await executor.run_task(task, package, directory / task["task_id"],
                            ExecutorOptions("openai", model="runtime-check"), smoke=smoke))
                    result["run_directory"] = str(directory)
                    return rows
                result["results"] = asyncio.run(runtime())
                result["passed"] = all(row["status"] == "completed" and row["resources_closed"] is True for row in result["results"])
                save(Path(result["run_directory"]) / "receipt.json", result)
        else:
            options = ExecutorOptions(provider=args.provider, model=args.model, concurrency=args.concurrency,
                max_turns=args.max_turns, max_output_tokens=args.max_output_tokens,
                task_timeout=args.task_timeout, request_timeout=args.request_timeout, skill_version=args.skill_version,
                **({"run_id": args.run_id} if args.run_id else {}))
            result = ExecutorService(default_executor_registry(), project_root=ROOT).run(tasks, options=options, execute=args.execute)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
        print(json.dumps(result, ensure_ascii=False))
        if result.get("passed") is False or result.get("status") in ("completed_with_errors", "interrupted"):
            return 2
        return 0
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
