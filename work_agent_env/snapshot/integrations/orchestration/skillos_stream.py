"""Online task stream with three real local SkillOS roles and a fixed API Executor."""
from dataclasses import replace
import asyncio
import json
from types import SimpleNamespace

from integrations.benchmark_tasks import make_task_set
from integrations.benchmark_tasks.store import save
from integrations.executors import ExecutorOptions
from integrations.executors.api import APIExecutor
from integrations.executors.diagnostics import error_details
from integrations.experience import ExperienceStore
from integrations.skill_generator.editor import SkillEditor
from integrations.skill_generator.roles import SkillOSRoles, load_roles
from integrations.skill_library import SkillLibrary
from training.markdown_contract import native_prompt


async def _run_partition(args, tasks, output, receipt, *, root, shared_roles=None, stop_state=None):
    roles = shared_roles or SkillOSRoles(load_roles(args.roles_config))
    stop_state = stop_state or {"event": asyncio.Event(), "error": None}
    store = ExperienceStore(root / ".runtime/experience")
    library = SkillLibrary(root / ".runtime/skill_library")
    editor = SkillEditor(store, library)
    backend = APIExecutor(project_root=root)
    options = ExecutorOptions(provider=args.provider, model=args.model, concurrency=1,
        max_turns=args.max_turns, max_output_tokens=args.max_output_tokens,
        task_timeout=args.task_timeout, request_timeout=args.request_timeout)
    # Fixed executor identity must remain stable across every task and skill snapshot.
    executor_ids = set()
    channel = "trial_" + args.run_id
    if library.head(channel) is not None:
        raise ValueError("Fresh stream cannot reuse a skill branch")
    parent, expected_head, previous = library.empty_version, None, None
    try:
        receipt["preflight"] = await roles.preflight()
        save(output / "receipt.json", receipt)
        if not receipt["preflight"]["ready"]:
            receipt.update(status="blocked_role_preflight", actual_task_attempts=0)
            return 2
        for index, task in enumerate(tasks["tasks"], 1):
            if stop_state["event"].is_set():
                receipt["not_started"] = [item["uid"] for item in tasks["tasks"][index - 1:]]
                break
            directory = output / "tasks" / f"{index:02d}"
            directory.mkdir(parents=True, exist_ok=False)
            item = {"index": index, "task_uid": task["uid"], "status": "executing",
                    "evidence_directory": str(directory), "stream_run_id": args.run_id,
                    "input_skill_version": parent, "training_admitted": False}
            receipt["results"].append(item)
            save(output / "receipt.json", receipt)
            print(json.dumps({"event": "task_started", "index": index, "task_uid": task["uid"]}), flush=True)
            task_options = replace(options, run_id=args.run_id + f"_t{index:02d}", skill_version=parent)
            try:
                execution = await backend.execute_async(make_task_set([task]), task_options)
                row = execution["results"][0]
                save(directory / "execution.json", execution)
                detail = row.get("error_details") or {}
                if detail.get("status_code") in (401, 403):
                    stop_state["error"] = {"task_uid": row["task_uid"], "diagnostics": detail}
                    stop_state["event"].set()
                item.update(execution_status=row["status"], resources_closed=row.get("resources_closed", False))
                if not item["resources_closed"] or "experience" not in row:
                    raise ValueError("Task resources or execution evidence are incomplete")
                identity = row["experience"]["id"]
                record = store.get(identity)
                executor_ids.add(record["executor"]["fixed_executor_id"])
                if len(executor_ids) != 1:
                    raise ValueError("Fixed API Executor changed during stream")
                view = store.view(identity, max_steps=100, observation_chars=4000)
                item.update(experience_id=identity, original_evaluation=view["result"])
                if view["result"]["status"] != "measured":
                    raise ValueError("Unknown original evaluator result; no skill/reward fabrication")
                feedback = await roles.selfjudge(view, directory=directory / "roles", seed=args.seed + index * 10007 + 3001)
                save(directory / "selfjudge.json", feedback)
                item["selfjudge_success"] = feedback["parsed"]["success"]
                # Reward the preceding edit on a subsequent task, never on its own source task.
                if previous is not None:
                    assessed = await roles.assess(candidate=previous["candidate"], downstream_view=view,
                        prompt_ids=previous["prompt_ids"], directory=directory / "roles", seed=args.seed + index * 10007,
                        selfjudge_result=feedback)
                    save(directory / "previous_candidate_reward.json", assessed)
                    item["previous_candidate_reward"] = {"candidate_id": assessed["candidate_id"],
                        "components": assessed["components"], "reward": assessed["reward"]}
                    receipt["rewarded_candidates"] += 1
                # Preserve the actual frozen-judge response in the Curator input.
                for max_steps in (100, 50, 20, 10, 3, 1):
                    request = editor.prepare(identity, generator_policy_id="skillos-qwen3-8b-initial",
                        parent=parent, max_steps=max_steps, observation_chars=2000,
                        selfjudge_feedback={"role": "selfjudge", "model_revision": feedback["model_revision"],
                                            "verdict": feedback["parsed"]})
                    messages = native_prompt(request)
                    try:
                        roles.render("curator", messages, 4096)
                        break
                    except ValueError:
                        if max_steps == 1:
                            raise
                save(directory / "curator_request.json", request)
                reply = await roles.query("curator", messages, directory=directory / "roles",
                                          seed=args.seed + index * 10007 + 4001)
                save(directory / "curator_reply.json", reply)
                # Malformed/invalid operations get their real SkillOS function-call score,
                # rather than disappearing through successful-response-only filtering.
                candidate = editor.apply(request, reply["raw_text"], sampling={"kind": "local_role_diagnostic", "reply": reply},
                                         record_failures=True)
                save(directory / "candidate.json", candidate)
                next_version = library.publish(candidate["id"], expected_head=expected_head, channel=channel)
                parent, expected_head = next_version, next_version
                previous = {"candidate": candidate, "prompt_ids": reply["prompt_ids"]}
                item.update(status="completed", candidate_id=candidate["id"], skill_version=parent,
                    skill_count=len(library.get(parent)), operations=len(candidate["operations"]),
                    executed_operations=sum(op["executed"] for op in candidate["operations"]))
            except Exception as exc:
                item.update(status="failed", error=error_details(exc))
                # A failed step breaks the adjacency reward edge. Never score stale skills
                # as though the failed Curator step had generated them.
                previous = None
            finally:
                save(directory / "receipt.json", item)
                save(output / "receipt.json", receipt)
                print(json.dumps({"event": "task_finished", **item}, ensure_ascii=False), flush=True)
        save(output / "skills.json", library.get(parent))
        receipt.update(status="completed" if all(r["status"] == "completed" for r in receipt["results"]) else "completed_with_errors",
            fixed_executor_ids=sorted(executor_ids),
            actual_task_attempts=len(receipt["results"]), final_skill_version=parent,
            final_skill_count=len(library.get(parent)), resources_closed=all(r.get("resources_closed") for r in receipt["results"]))
        if stop_state["event"].is_set():
            receipt.update(status="blocked_executor_access", executor_access_error=stop_state["error"])
        receipt["summary"] = {"tasks": len(receipt["results"]),
            "valid_original_scores": sum(r.get("original_evaluation", {}).get("status") == "measured" for r in receipt["results"]),
            "original_passed": sum(r.get("original_evaluation", {}).get("passed") is True for r in receipt["results"]),
            "all_role_steps_completed": sum(r["status"] == "completed" for r in receipt["results"]),
            "rewarded_candidates": receipt["rewarded_candidates"]}
        return 0 if receipt["status"] == "completed" else 2
    finally:
        if shared_roles is None:
            await roles.close()
        save(output / "receipt.json", receipt)


async def run_stream(args, tasks, output, receipt, *, root):
    """Independent branches overlap API execution; role permits remain shared/bounded."""
    count = min(args.streams, len(tasks["tasks"]))
    if count == 1:
        return await _run_partition(args, tasks, output, receipt, root=root)
    roles = SkillOSRoles(load_roles(args.roles_config))
    stop_state = {"event": asyncio.Event(), "error": None}
    try:
        receipt["preflight"] = await roles.preflight()
        receipt["independent_streams"] = count
        receipt["last_candidates_without_future_reward"] = count
        receipt["streams"] = []
        save(output / "receipt.json", receipt)
        if not receipt["preflight"]["ready"]:
            receipt.update(status="blocked_role_preflight", actual_task_attempts=0)
            return 2
        length = len(tasks["tasks"])
        async def partition(index):
            selected = tasks["tasks"][index * length // count:(index + 1) * length // count]
            child_args = SimpleNamespace(**vars(args))
            child_args.run_id = args.run_id + f"_s{index + 1:02d}"
            child_output = output / "streams" / f"{index + 1:02d}"
            child_output.mkdir(parents=True, exist_ok=False)
            child_receipt = {"schema_version": "workagent-skillos-stream-v1", "run_id": child_args.run_id,
                "status": "running", "results": [], "rewarded_candidates": 0,
                "training_admitted": False, "actual_parameter_updates": 0}
            receipt["streams"].append({"run_id": child_args.run_id, "receipt": str(child_output / "receipt.json"),
                                       "tasks": [t["uid"] for t in selected]})
            save(output / "receipt.json", receipt)
            await _run_partition(child_args, make_task_set(selected), child_output, child_receipt, root=root,
                                 shared_roles=roles, stop_state=stop_state)
            return child_receipt
        # gather's normal path waits for every environment to close. On cancellation,
        # join the cancelled children before closing the shared model HTTP client.
        workers = [asyncio.create_task(partition(index)) for index in range(count)]
        try:
            children = await asyncio.gather(*workers)
        except BaseException:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        receipt["results"] = [row for child in children for row in child["results"]]
        receipt["rewarded_candidates"] = sum(child["rewarded_candidates"] for child in children)
        receipt["summary"] = {key: sum(child["summary"][key] for child in children) for key in
            ("tasks", "valid_original_scores", "original_passed", "all_role_steps_completed", "rewarded_candidates")}
        receipt.update(status="completed" if all(c["status"] == "completed" for c in children) else "completed_with_errors",
            actual_task_attempts=len(receipt["results"]), resources_closed=all(c["resources_closed"] for c in children),
            final_skill_versions=[c["final_skill_version"] for c in children])
        receipt["fixed_executor_ids"] = sorted({identity for c in children for identity in c["fixed_executor_ids"]})
        if len(receipt["fixed_executor_ids"]) > 1:
            receipt.update(status="failed_executor_identity_mismatch")
        if stop_state["event"].is_set():
            receipt.update(status="blocked_executor_access", executor_access_error=stop_state["error"],
                not_started=[uid for child in children for uid in child.get("not_started", [])])
        return 0 if receipt["status"] == "completed" else 2
    finally:
        await roles.close()
        save(output / "receipt.json", receipt)
