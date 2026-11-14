"""20-task online SkillOS stream: local Curator/judges and a fixed API Executor."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.benchmark_tasks import identifier, load_task_set, make_task_set
from integrations.benchmark_tasks.store import TaskStore, save
from integrations.orchestration.skillos_stream import run_stream
from integrations.skill_generator.roles import load_roles


def load_credentials(path):
    """Read literal model settings without evaluating shell or printing secrets."""
    for raw in Path(path).read_text().splitlines():
        line = raw.strip().removeprefix("export ")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in {"MODEL_API_KEY", "MODEL_API_URL", "MODEL"}:
            continue
        parts = shlex.split(value, comments=True)
        if len(parts) != 1:
            raise ValueError("Invalid literal model setting")
        os.environ[key] = parts[0]
    if not os.environ.get("MODEL_API_KEY"):
        raise ValueError("Model credentials unavailable")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", default="toolathon-gym")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--streams", type=int, default=4, help="Independent online skill branches; task environments overlap across streams")
    parser.add_argument("--task", action="append")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--roles-config", type=Path, default=ROOT / "configs/skillos_roles.json")
    parser.add_argument("--provider", choices=("openai", "anthropic"), default="openai")
    parser.add_argument("--model", default="gpt-6.1-sol", help="Fixed external Executor only")
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--task-timeout", type=float, default=1800)
    parser.add_argument("--request-timeout", type=float, default=180)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--legacy-diagnostic", action="store_true",
                        help="Explicitly opt into the superseded online diagnostic; use run_skillos_upstream.py for source204")
    args = parser.parse_args()
    if args.execute and not args.legacy_diagnostic:
        parser.error("This historical stream is superseded. Use scripts/run_skillos_upstream.py; "
                     "--legacy-diagnostic is required only for intentional historical diagnostics.")
    identifier(args.run_id)
    roles_config = load_roles(args.roles_config)
    store = TaskStore(ROOT)
    registration = store.register(args.benchmark)
    full = load_task_set(registration["task_set"])
    if not 1 <= args.count <= len(full["tasks"]):
        raise ValueError("Invalid task count")
    if args.streams < 1:
        raise ValueError("Stream count must be positive")
    selected = full["tasks"][:args.count]
    if args.task:
        wanted = set(args.task)
        if len(wanted) != len(args.task) or not wanted <= {t["task_id"] for t in full["tasks"]}:
            raise ValueError("Unknown or duplicate task IDs")
        selected = [t for t in full["tasks"] if t["task_id"] in wanted]
    tasks = make_task_set(selected)
    if not args.execute:
        print(json.dumps({"status": "planned_not_executed", "tasks": [t["uid"] for t in tasks["tasks"]],
            "role_models": {k: v["model_family"] for k, v in roles_config["roles"].items()},
            "independent_streams": min(args.streams, len(tasks["tasks"])),
            "mode": "online_stream_diagnostic_not_grpo", "actual_parameter_updates": 0}, ensure_ascii=False))
        return 0
    load_credentials(ROOT / ".runtime/private/gpt-api.env")
    output = ROOT / ".runtime/pipelines" / args.run_id
    output.mkdir(parents=True, exist_ok=False)
    save(output / "tasks.json", tasks)
    save(output / "registration.json", registration)
    save(output / "roles_config.json", roles_config)
    save(output / "model_sources.json", {name: json.loads(path.read_text()) if path.is_file() else None
        for name, role in roles_config["roles"].items()
        for path in [ROOT / role["model_path"] / "deployment_source.json"]})
    sources = [Path(__file__), ROOT / "configs/skillos_roles.json", ROOT / "training/markdown_contract.py",
               ROOT / "scripts/serve_skillos_content.py", ROOT / "scripts/skill_os_services.py",
               ROOT / "scripts/generator_python.sh", ROOT / "configs/skillos-content.requirements.in"]
    for name in ("experience", "skill_generator", "skill_library", "executors", "environments", "verifiers", "orchestration"):
        sources.extend((ROOT / "integrations" / name).rglob("*.py"))
    for source in sources:
        target = output / "source_snapshot" / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    receipt = {"schema_version": "workagent-skillos-stream-v1", "run_id": args.run_id,
        "options": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "observed_at": datetime.now(timezone.utc).isoformat(), "status": "running",
        "selection": "registered_order_diagnostic_not_reviewed_training_pairs", "tasks_sha256": tasks["tasks_sha256"],
        "results": [], "rewarded_candidates": 0, "training_admitted": False,
        "actual_parameter_updates": 0, "mode": "online_stream_diagnostic_not_grpo",
        "last_candidate_has_no_future_task_reward": True,
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}}
    save(output / "receipt.json", receipt)
    try:
        return asyncio.run(run_stream(args, tasks, output, receipt, root=ROOT))
    except BaseException as exc:
        receipt.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error_type=type(exc).__name__)
        raise
    finally:
        save(output / "receipt.json", receipt)
        print(json.dumps({"run_id": args.run_id, "status": receipt["status"], "summary": receipt.get("summary"),
                          "receipt": str(output / "receipt.json")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
