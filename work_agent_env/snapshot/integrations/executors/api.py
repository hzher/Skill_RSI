"""Dataset-neutral API model loop, bounded concurrency and per-task receipts."""
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from integrations.benchmark_tasks import digest, validate_task_set
from integrations.benchmark_tasks.store import TaskStore, save
from integrations.environments.docker import DockerEnvironment
from integrations.verifiers import VerifierRegistry, VerifierService
from .models import APIModel
from .tools import ToolDispatcher
from .diagnostics import error_details, redact


def stamp():
    return datetime.now(timezone.utc).isoformat()


class APIExecutor:
    def __init__(self, *, project_root, model_factory=APIModel, environment_factory=DockerEnvironment):
        self.root = Path(project_root).resolve()
        self.store = TaskStore(self.root)
        self.model_factory, self.environment_factory = model_factory, environment_factory

    def plan(self, task_set, options):
        tasks = validate_task_set(task_set)
        if options.environment_image is not None:
            raise ValueError("Declare the environment image in the versioned task package")
        model = options.model or os.environ.get("MODEL")
        if not model:
            raise ValueError("An explicit fixed --model or MODEL is required")
        packages = {}
        package_cache = {}
        for task in tasks["tasks"]:
            packages[task["uid"]] = str(self.store.package_for(task, cache=package_cache))
        if not callable(self.model_factory) or not callable(self.environment_factory):
            raise ValueError("Invalid runtime factories")
        sources = {}
        source_root = Path(__file__).resolve().parents[1]
        for directory in ("benchmark_tasks", "executors", "environments", "verifiers", "experience", "skill_library", "skill_generator/vendor"):
            for path in (source_root / directory).rglob("*.py"):
                sources[path.relative_to(source_root.parent).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        skills = {}
        if options.skill_version is not None:
            from integrations.skill_library import SkillLibrary
            skills = SkillLibrary(self.root / ".runtime/skill_library").get(options.skill_version)
        return {"schema_version": "workagent-execution-plan-v2", "status": "planned_not_executed",
            "executor": "api", "run_id": options.run_id, "options": {**asdict(options), "model": model},
            "tasks_sha256": tasks["tasks_sha256"], "tasks": len(tasks["tasks"]), "packages": packages,
            "run_directory": str(self.root / ".runtime/executors" / options.run_id), "source_sha256": sources,
            "endpoint_sha256": hashlib.sha256(os.environ.get("MODEL_API_URL", options.provider).encode()).hexdigest(),
            "skill_snapshot": skills}

    def execute(self, task_set, options):
        return asyncio.run(self.execute_async(task_set, options))

    async def freeze_plan(self, task_set, options):
        """Resolve exactly the same frozen Executor identity without starting a task/model."""
        tasks = validate_task_set(task_set)
        plan = self.plan(tasks, options)
        options = replace(options, model=plan["options"]["model"])
        directory = Path(plan["run_directory"])
        images = {}
        if isinstance(self.environment_factory, type) and issubclass(self.environment_factory, DockerEnvironment):
            probe = DockerEnvironment(task=tasks["tasks"][0], package=Path(plan["packages"][tasks["tasks"][0]["uid"]]), directory=directory)
            for task in tasks["tasks"]:
                config = task["environment"]["config"]
                for name in [config["image"]] + [service["image"] for service in config.get("services", [])]:
                    if name not in images:
                        _, identity, _ = await probe.command("image", "inspect", "--format", "{{.Id}}", name)
                        images[name] = identity.strip()
        plan["resolved_images"] = images
        plan["fixed_executor_id"] = digest({"source_sha256": plan["source_sha256"], "images": images,
            "provider": options.provider, "model": options.model, "endpoint_sha256": plan["endpoint_sha256"],
            "max_turns": options.max_turns, "max_output_tokens": options.max_output_tokens,
            "tool_scope": options.tool_scope, "request_timeout": options.request_timeout, "task_timeout": options.task_timeout})
        return plan

    async def execute_async(self, task_set, options):
        tasks = validate_task_set(task_set)
        if self.model_factory is APIModel and not os.environ.get("MODEL_API_KEY"):
            raise ValueError("MODEL_API_KEY is required for execution")
        plan = await self.freeze_plan(tasks, options)
        options = replace(options, model=plan["options"]["model"])
        directory = Path(plan["run_directory"])
        images = plan["resolved_images"]
        directory.mkdir(parents=True, exist_ok=False)
        save(directory / "tasks.json", tasks)
        save(directory / "plan.json", plan)
        for name, expected in plan["source_sha256"].items():
            source = Path(__file__).resolve().parents[2] / name
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise ValueError("Executor source changed before execution: " + name)
            target = directory / "source_snapshot" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        receipt = {"schema_version": "workagent-executor-receipt-v2", "run_id": options.run_id,
            "tasks_sha256": tasks["tasks_sha256"], "status": "running", "started_at": stamp(),
            "model": options.model, "provider": options.provider, "results": [], "resources_closed": None,
            "training_admitted": False, "receipt_path": str(directory / "receipt.json")}
        save(directory / "receipt.json", receipt)
        semaphore = asyncio.Semaphore(options.concurrency)
        async def owned(task):
            async with semaphore:
                return await self.run_task(task, Path(plan["packages"][task["uid"]]), directory / digest(task), options,
                    images=images, skill_snapshot=plan["skill_snapshot"], fixed_executor_id=plan["fixed_executor_id"])
        jobs = [asyncio.create_task(owned(task)) for task in tasks["tasks"]]
        try:
            receipt["results"] = await asyncio.gather(*jobs)
            receipt["resources_closed"] = all(row["resources_closed"] is True for row in receipt["results"])
            receipt["experience_complete"] = all("experience" in row and "experience_error" not in row for row in receipt["results"])
            receipt["status"] = "completed" if receipt["experience_complete"] and all(row["status"] == "completed" for row in receipt["results"]) else "completed_with_errors"
        except BaseException:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            receipt["status"] = "interrupted"
            raise
        finally:
            receipt["observed_at"] = stamp()
            save(directory / "receipt.json", receipt)
        return receipt

    async def run_task(self, task, package, directory, options, *, smoke=None, images=None,
                       skill_snapshot=None, fixed_executor_id=None):
        directory.mkdir(parents=True, exist_ok=False)
        environment = self.environment_factory(task=task, package=package, directory=directory, images=images)
        model, history, calls = None, [], []
        public_call_id = None
        started = time.monotonic()
        async def recorded_call(name, arguments):
            event = {"name": name, "arguments": arguments, "started_at": stamp(), "public_call_id": public_call_id}
            call_started = time.monotonic()
            try:
                output = await environment.call(name, arguments)
                event["output"] = output
                return output
            except BaseException as exc:
                event["error"] = type(exc).__name__
                raise
            finally:
                event["duration_seconds"] = time.monotonic() - call_started
                calls.append(event)
                save(directory / "tool_calls.json", calls)
        result = {"task_uid": task["uid"], "task_sha256": digest(task), "status": "running",
            "evaluation": None, "error": None, "resources_closed": None,
            "stage": "initializing", "model_requests_attempted": 0, "model_replies": 0,
            "http_requests_attempted": 0,
            "trajectory": str(directory / "trajectory.json"), "training_admitted": False}
        save(directory / "receipt.json", result)
        try:
            async with asyncio.timeout(options.task_timeout):
                result["environment"] = await environment.initialize()
                tools = environment.discover()
                if options.tool_scope == "task":
                    required = {tool["name"] for tool in task["tools"]}
                    tools = [tool for tool in tools if tool["name"] in required or any(
                        tool["name"].startswith(name + "__") for name in required)]
                allowed = {tool["name"] for tool in tools}
                if not allowed:
                    raise ValueError("No tools available in the selected scope")
                history = [{"role": "user", "content": task["prompt"]}]
                if smoke is not None:
                    for call in smoke.get("calls", []):
                        if call["name"] not in allowed:
                            raise ValueError("Smoke test selected an undeclared tool")
                        output = await recorded_call(call["name"], call["arguments"])
                        history.append({"role": "smoke", "name": call["name"], "output": output})
                else:
                    model = self.model_factory(options)
                    system = (task["system_prompt"] or "Complete the task using the provided tools.") + "\nYour task workspace is /workspace."
                    if options.skill_version is not None:
                        from integrations.skill_generator.vendor.reward_toolchain import SkillRepo
                        if SkillRepo(skill_snapshot or {}).digest() != options.skill_version:
                            raise ValueError("Frozen skill snapshot differs from selected version")
                        system += "\n\n## Reusable skills\n" + "\n\n".join(
                            skill_snapshot[name] for name in sorted(skill_snapshot))
                    dispatcher = ToolDispatcher(tools, recorded_call)
                    if dispatcher.searchable:
                        system += "\nUse search_available_tools to find tools and argument schemas, then call_available_tool to execute them. Browse all pages when needed."
                    definitions = dispatcher.definitions()
                    public_names = {tool["name"] for tool in definitions}
                    for turn in range(options.max_turns):
                        result.update(stage="model_request", turn=turn + 1,
                            model_requests_attempted=result["model_requests_attempted"] + 1)
                        save(directory / "receipt.json", result)
                        reply = await model.turn(system, definitions, history)
                        result["model_replies"] += 1
                        result["http_requests_attempted"] += len(reply.get("request_diagnostics", {}).get("attempts", []))
                        history.append({"role": "assistant", **reply})
                        save(directory / "trajectory.json", history)
                        if not reply["calls"]:
                            break
                        for call in reply["calls"]:
                            public_call_id = call["id"]
                            result["stage"] = "tool_call"
                            try:
                                if call["name"] not in public_names:
                                    raise ValueError("Model selected an unavailable tool")
                                output = await dispatcher.call(call["name"], call["arguments"])
                            except (ValueError, KeyError) as exc:
                                output = {"tool_error": type(exc).__name__, "message": redact(exc),
                                    "instruction": "Inspect the tool schema, correct the named arguments, then retry."}
                            content = json.dumps(output, ensure_ascii=False, allow_nan=False)
                            # Full outputs stay in the trajectory; model previews have a bounded size.
                            if len(content) > 100000:
                                identifier = hashlib.sha256(content.encode()).hexdigest()
                                saved = directory / "workspace/.executor_outputs" / (identifier + ".json")
                                saved.parent.mkdir(parents=True, exist_ok=True)
                                saved.write_text(content, encoding="utf-8")
                                content = json.dumps({"preview": content[:12000], "characters": len(content),
                                    "full_result_path": "/workspace/.executor_outputs/" + saved.name,
                                    "instruction": "Full result saved; read it using Python or filesystem tools. Preview is incomplete."}, ensure_ascii=False)
                            history.append({"role": "tool", "id": call["id"], "content": content})
                    else:
                        result["model_stop_reason"] = "turn_budget_exhausted"
                registry = VerifierRegistry()
                result["stage"] = "evaluation"
                save(directory / "receipt.json", result)
                async def original_evaluator(selected_task, context):
                    return await environment.evaluate()
                registry.register(task["evaluation"]["backend"], original_evaluator,
                    version=task["source"]["package_sha256"] + ":" + result["environment"]["image_id"])
                result["evaluation"] = await VerifierService(registry,
                    timeout=task["evaluation"]["config"].get("timeout", 180)).evaluate(task, {})
                result["status"] = "completed" if result["evaluation"]["valid"] else "evaluation_unknown"
                if smoke is not None and "passed" in smoke and result["evaluation"]["passed"] is not smoke["passed"]:
                    result.update(status="smoke_mismatch", error="Smoke evaluator expectation differs")
        except Exception as exc:
            result.update(status="execution_unknown", error=type(exc).__name__)
            result["failure_stage"] = result["stage"]
            result["error_details"] = error_details(exc)
            diagnostic = getattr(exc, "request_diagnostics", getattr(model, "last_request", None))
            if diagnostic is not None:
                result["request_diagnostics"] = diagnostic
                if result["failure_stage"] == "model_request" and result["model_requests_attempted"] > result["model_replies"]:
                    result["http_requests_attempted"] += len(diagnostic.get("attempts", []))
        except asyncio.CancelledError:
            result.update(status="interrupted", error="CancelledError")
            raise
        finally:
            result["stage"] = "closing"
            save(directory / "trajectory.json", history)
            try:
                result["close"] = await environment.close()
                result["resources_closed"] = result["close"]["resources_closed"]
                if result["status"] == "completed" and not result["resources_closed"]:
                    result["status"] = "cleanup_unknown"
            finally:
                if model is not None:
                    try:
                        async with asyncio.timeout(10):
                            await model.close()
                    except Exception:
                        result["model_close_unknown"] = True
                result["observed_at"] = stamp()
                result["stage"] = "finished"
                result["duration_seconds"] = time.monotonic() - started
                from integrations.experience import ExperienceStore
                try:
                    result["experience"] = ExperienceStore(self.root / ".runtime/experience").capture(
                        task=task, receipt=result, trajectory=history, calls=calls, directory=directory,
                        executor={"backend": "api", "run_id": options.run_id, "provider": options.provider,
                                  "model": options.model, "plan_ref": str(directory.parent / "plan.json"),
                                  "fixed_executor_id": fixed_executor_id, "skill_version": options.skill_version,
                                  "plan_sha256": hashlib.sha256((directory.parent / "plan.json").read_bytes()).hexdigest()
                                      if (directory.parent / "plan.json").is_file() else None,
                                  "capture_kind": "smoke" if smoke is not None else "api_execution"})
                except Exception as exc:
                    result["experience_error"] = type(exc).__name__
                save(directory / "receipt.json", result)
        return result
