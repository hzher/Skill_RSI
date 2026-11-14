#!/usr/bin/env python3
"""Experience ingestion, SkillOS editing, library publication and attribution."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integrations.benchmark_tasks.contracts import digest, load_task_set, strict_json
from integrations.experience import ExperienceStore, curator_view
from integrations.skill_library import SkillLibrary
from integrations.skill_generator.editor import SkillEditor
from integrations.skill_generator.attributor import attribute


def read(path):
    return strict_json(Path(path).read_text(encoding="utf-8"))


def ingest_run(store, directory):
    directory = Path(directory).resolve()
    tasks = load_task_set(directory / "tasks.json")
    plan = read(directory / "plan.json")
    if tasks["tasks_sha256"] != plan["tasks_sha256"]:
        raise ValueError("Run task-set identity differs")
    # Old runs have source snapshots too. Verify rather than borrowing current code identity.
    import hashlib
    for name, expected in plan["source_sha256"].items():
        source = (directory / "source_snapshot" / name).resolve()
        if not source.is_relative_to(directory / "source_snapshot") or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError("Historical source snapshot identity differs")
    results = []
    for task in tasks["tasks"]:
        episode = directory / digest(task)
        receipt = read(episode / "receipt.json")
        result = store.capture(task=task, receipt=receipt, trajectory=read(episode / "trajectory.json"),
            calls=read(episode / "tool_calls.json") if (episode / "tool_calls.json").exists() else [],
            directory=episode, executor={"backend": "api", "run_id": plan["run_id"],
                "provider": plan["options"]["provider"], "model": plan["options"]["model"],
                "plan_ref": str(directory / "plan.json"), "plan_sha256": hashlib.sha256((directory / "plan.json").read_bytes()).hexdigest(),
                "fixed_executor_id": plan.get("fixed_executor_id"), "skill_version": plan["options"].get("skill_version"),
                "capture_kind": "historical_api_import"})
        results.append({"task_uid": task["uid"], **result})
    return {"imported": len(results), "results": results, "training_admitted": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, help="New JSON file; existing files are never replaced")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest-run")
    ingest.add_argument("--run-directory", type=Path, required=True)
    view = commands.add_parser("view")
    view.add_argument("--experience", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--experience", required=True)
    prepare.add_argument("--generator-policy-id", required=True)
    prepare.add_argument("--parent")
    apply = commands.add_parser("apply")
    apply.add_argument("--request", type=Path, required=True)
    apply.add_argument("--response", type=Path, required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--experience", required=True)
    generate.add_argument("--generator-policy-id", required=True)
    generate.add_argument("--provider", required=True, choices=("openai", "anthropic"))
    generate.add_argument("--model", required=True)
    search = commands.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--version")
    publish = commands.add_parser("publish")
    publish.add_argument("--candidate", required=True)
    publish.add_argument("--expected-head", required=True, help="Use 'empty' for a new channel")
    publish.add_argument("--channel", default="main")
    attribution = commands.add_parser("attribute")
    attribution.add_argument("--candidate", required=True)
    attribution.add_argument("--experience", required=True)
    attribution.add_argument("--fixed-executor-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.output is not None and args.output.exists():
            raise FileExistsError("Output already exists")
        store = ExperienceStore(args.project_root / ".runtime/experience")
        library = SkillLibrary(args.project_root / ".runtime/skill_library")
        editor = SkillEditor(store, library)
        if args.command == "ingest-run":
            result = ingest_run(store, args.run_directory)
        elif args.command == "view":
            result = store.view(args.experience)
        elif args.command == "prepare":
            result = editor.prepare(args.experience, generator_policy_id=args.generator_policy_id, parent=args.parent)
        elif args.command == "apply":
            result = editor.apply(read(args.request), args.response.read_text(encoding="utf-8"))
        elif args.command == "generate":
            from integrations.executors.models import APIModel
            from integrations.executors import ExecutorOptions
            async def generate_candidate():
                model = APIModel(ExecutorOptions(provider=args.provider, model=args.model))
                try:
                    return await editor.generate(args.experience, model=model, generator_policy_id=args.generator_policy_id)
                finally:
                    await model.close()
            result = asyncio.run(generate_candidate())
        elif args.command == "search":
            result = library.retrieve(args.query, version=args.version)
        elif args.command == "publish":
            result = {"version": library.publish(args.candidate, channel=args.channel,
                expected_head=None if args.expected_head == "empty" else args.expected_head)}
        else:
            result = attribute(library.get_candidate(args.candidate), store.get(args.experience),
                fixed_executor_id=args.fixed_executor_id)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
            print(json.dumps({"output": str(args.output), "command": args.command}))
        else:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, KeyError, OSError) as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
