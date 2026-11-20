"""An ordered CL-Bench family on one native task and one restricted Codex thread."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict
from itertools import groupby
from pathlib import Path
import re
import shutil
import sys
import time
import uuid
from unittest.mock import patch

from . import codex_chatgpt as actor
from .clbench import CLBenchTask, capture_workspace
from .interactive import ActionValidationError
from .storage import PendingStage, atomic, begin, identifier, seal
from .vendor.common import canonical, file_sha, immutable, load


def validate_stateful_plan(tasks):
    if not tasks:
        raise ValueError("A stateful collection requires at least one task")
    seen_families, seen_tasks = set(), set()
    for family, members in groupby(tasks, key=lambda row: identifier(row["family"])):
        if family in seen_families:
            raise ValueError("A stateful family must be contiguous")
        seen_families.add(family)
        for index, row in enumerate(members):
            task_id = identifier(row["id"])
            if (type(row["canonical_index"]) is not int or row["canonical_index"] != index
                    or row.get("schedule", "default") != "default" or task_id in seen_tasks):
                raise ValueError("Stateful plans require distinct tasks in canonical order from zero")
            seen_tasks.add(task_id)


class Family:
    def __init__(self, name, directory):
        self.name, self.directory = name, directory
        self.task = self.query = self.episode = None
        self.terminal = False
        self.receipts = {}
        self.owned = set()
        self.hooks = ExitStack()

    def start(self, size):
        from src.registry import get_task_class
        self.task = get_task_class(self.name)(schedule="default")
        self.task.prepare_run(0)
        if self.name in ("sales_prediction", "codebase_adaptation"):
            original = self.task._next_query

            def before_rotation():
                if self.episode and (getattr(self.task, "_new_issue_pending", False)
                                     or getattr(self.task, "_new_instance_pending", False)):
                    self.episode.capture()
                try:
                    return original()
                finally:
                    self.track(self.container)
            self.hooks.enter_context(patch.object(self.task, "_next_query", before_rotation))
        if self.name == "codebase_adaptation":
            from src.tasks.codebase_adaptation import generic_runtime
            create = generic_runtime._start_generic_container

            def track_verifier(*args, **kwargs):
                container = create(*args, **kwargs)
                self.track(container)
                return container
            self.hooks.enter_context(patch.object(generic_runtime, "_start_generic_container", track_verifier))
        if self.name == "database_exploration":
            swap = self.task._swap_to_drifted_db

            def before_swap():
                if self.episode:
                    self.episode.capture()
                return swap()
            self.hooks.enter_context(patch.object(self.task, "_swap_to_drifted_db", before_swap))
        self.query = self.task.reset()
        self.track(self.container)
        if size > len(self.task.instances):
            raise ValueError("Plan exceeds the official family size")
        self.collect()
        self.terminal = bool((self.query.metadata or {}).get("done"))

    @property
    def container(self):
        return getattr(getattr(self.task, "_env", None), "container_id", None)

    @property
    def cwd(self):
        env = getattr(self.task, "_env", None)
        return getattr(getattr(env, "config", None), "cwd", None) or "/workspace"

    def track(self, container):
        if container and container not in self.owned:
            if not re.fullmatch(r"[0-9a-f]{12,64}", container):
                raise ValueError("Invalid native container ID")
            self.owned.add(container)
            atomic(self.directory / "owned-containers.json", sorted(self.owned))

    def collect(self, latest=None):
        outcomes = list(self.task.get_instance_outcomes())
        if latest is not None:
            outcomes.append(latest)
        for outcome in outcomes:
            value = asdict(outcome)
            idx = value["instance_index"]
            if idx in self.receipts and self.receipts[idx] != value:
                raise RuntimeError("Native verdict changed for an earlier instance")
            self.receipts[idx] = value

    def evaluate(self):
        result = asdict(self.task.evaluate())
        atomic(self.directory / "native-objective.json", result)
        outcomes = result["instance_outcomes"]
        if (len(outcomes) != len(self.receipts)
                or {v["instance_index"]: v for v in outcomes} != self.receipts):
            raise RuntimeError("Family evaluator disagrees with the per-instance verdicts")

    def close(self):
        try:
            self.track(self.container)
            CLBenchTask.close(self)
        finally:
            self.hooks.close()


class Episode:
    def __init__(self, family, index, path):
        self.family, self.index, self.path = family, index, path
        self.actions = 0
        self.cwd = family.cwd
        self.auto = index in family.receipts
        if self.auto:
            self.instance_id = family.receipts[index]["instance_id"]
        else:
            if family.terminal or family.query is None or family.query.instance_index != index:
                raise RuntimeError("Native query does not match the next planned instance")
            self.instance_id = family.query.instance_id
        family.episode = self

    @property
    def done(self):
        return self.index in self.family.receipts

    def status(self):
        # Stable native identifiers may encode hidden opponent policies or stages.
        query = None if self.done else self.family.query
        return {"done": self.done, "cwd": self.cwd, "query": query.prompt if query else None,
                "response_schema": query.response_schema.model_json_schema() if query else None}

    def act(self, action):
        from src.interface import Response
        if self.done:
            raise ActionValidationError("This instance has finished")
        try:
            validated = self.family.query.response_schema.model_validate(action)
        except ValueError as error:
            raise ActionValidationError(str(error)) from error
        step = self.family.task.step(Response(action=validated))
        self.actions += 1
        self.family.collect(step.instance_outcome)
        self.family.query, self.family.terminal = step.next_query, step.done
        if not self.done and (step.done or step.next_query is None or step.next_query.instance_index != self.index):
            raise RuntimeError("Task advanced without a verdict for its current instance")
        return {"done": self.done, "action": validated.model_dump(mode="json"),
                "observation": asdict(step.observation), "current": self.status()}

    def capture(self):
        if (self.path / "workspace-capture.json").exists():
            if self.family.container and not self.auto:
                if load(self.path / "workspace-capture.json").get("exit_code") != 0:
                    raise RuntimeError("An earlier workspace capture failed")
            return
        if self.family.container and not self.auto:
            if not capture_workspace(self.family.container, self.family.cwd, self.path):
                raise RuntimeError("Could not freeze the outgoing native workspace")
        else:
            atomic(self.path / "workspace-capture.json", {"kind": "official_in_memory_task",
                  "filesystem_workspace": False, "captured": True})
        db = getattr(self.family.task, "_temp_db_path", None)
        if db and Path(db).is_file():
            atomic(self.path / "database-state.json", {"sha256": actor.digest(db), "bytes": Path(db).stat().st_size,
                  "snapshot_kind": "state_digest", "full_state_archived": False})


def usage_delta(current, previous):
    if not isinstance(current, dict) or not current or not isinstance(previous, dict):
        return None
    if previous and current.keys() != previous.keys():
        return None
    if any(type(n) is not int or n < 0 for n in (*current.values(), *previous.values())):
        return None
    if any(n < previous.get(k, 0) for k, n in current.items()):
        return None
    return {k: n - previous.get(k, 0) for k, n in current.items()}


def execute_case(config, task, path, family, owner, previous_id, thread, previous_usage):
    started = time.time()
    index = task["canonical_index"]
    intent = {"family": family.name, "index": index, "backend": "codex", "auth_mode": "chatgpt",
              "execution_mode": "stateful", "protocol": "native-mcp-v1", "configuration": config,
              "previous_attempt_id": previous_id}
    if begin(path, intent) is not None:
        raise RuntimeError("Cannot attach a sealed case to a new live family")
    episode = owner.submit(Episode, family, index, path).result()
    identity = {"family": family.name, "task_id": task["id"], "canonical_index": index,
                "instance_id": episode.instance_id, "schedule": "default", "attempt_id": uuid.uuid4().hex}
    atomic(path / "identity.json", identity)
    initial = owner.submit(episode.status).result()
    atomic(path / "initial-task.json", initial)
    atomic(path / "task-environment.json", {"execution_mode": "stateful", "reset_scope": "family",
          "cwd": family.cwd, "container_id": family.container, "tools": ["benchmark.status", "benchmark.act"]})
    native = {"complete": True, "tool_calls": 0, "usage": None, "errors": []}
    threads = []
    broker_error = None
    failure = None
    if not episode.auto:
        workspace = family.directory / "workspace"
        workspace.mkdir(exist_ok=True)
        try:
            native, threads, broker_error = actor.run_actor(
                config, path, episode, owner, actor.task_prompt(initial), family_workspace=workspace,
                resume_thread_id=thread)
            if (len(threads) != 1 or (thread and threads[0] != thread)
                    or str(uuid.UUID(threads[0])) != threads[0]):
                raise RuntimeError("Codex did not confirm the expected family thread")
        except Exception as error:
            # A turn may already have applied its terminal action. Preserve its
            # native verdict and journal even when transport or continuity failed.
            failure = {"kind": type(error).__name__, "message": str(error)}
            atomic(path / "failure.json", failure)
            native["complete"] = False
            threads = []
    current = threads[0] if threads else thread
    continuity = {"family": family.name, "canonical_index": index, "attempt_id": identity["attempt_id"],
                  "previous_attempt_id": previous_id, "thread_id": current,
                  "resumed_thread_id": thread if threads else None, "session_started_here": bool(threads and not thread),
                  "initialization_terminal": episode.auto, "actor_attempts": int(not episode.auto),
                  "family_close_ref": f"families/{family.name}/close-receipt.json"}
    atomic(path / "continuity.json", continuity)
    atomic(path / "final-task.json", owner.submit(episode.status).result())
    owner.submit(episode.capture).result()
    manifest = actor.artifact_manifest(path)
    manifest["continuity.json"] = {"sha256": file_sha(path / "continuity.json"),
                                   "bytes": (path / "continuity.json").stat().st_size}
    atomic(path / "artifact-manifest.json", manifest)
    digest = file_sha(path / "artifact-manifest.json")
    objective = {"instance_outcomes": [family.receipts[index]] if episode.done else []}
    atomic(path / "native-objective.json", objective)
    matched, mapping = actor.match_outcome(objective, identity)
    score = int(matched["success"]) if type(matched.get("success")) is bool else None
    atomic(path / "scoring-link.json", {"identity": identity, "mapping": mapping,
          "query_instance_id": episode.instance_id, "outcome_instance_id": matched.get("instance_id"),
          "artifact_manifest_sha256": digest, "verifier_receipt_sha256": file_sha(path / "native-objective.json")})
    atomic(path / "close-receipt.json", {"complete": True, "scope": "instance_transport",
          "native_task_scope": "family", "family_close_ref": continuity["family_close_ref"]})
    journal = actor.check_journals(path, episode.actions)
    complete = bool(episode.done and native["complete"] and not broker_error and journal["journal_complete"])
    delta = usage_delta(native.get("usage"), previous_usage) if threads else None
    record = {**intent, "identity": identity, "native": native, "native_thread_ids": threads,
              "execution_complete": complete, "complete": complete and score is not None, "task_done": episode.done,
              "initialization_terminal": episode.auto, "task_actions": episode.actions, "continuity": continuity,
              "native_usage_scope": "thread_cumulative", "usage_delta": delta,
              "usage_delta_scope": "invocation_delta" if delta is not None else None,
              "objective": objective, "score_binding_version": 1, "score_mapping": mapping, "score": score,
              "scoring_status": "scored" if score is not None else "unavailable", "artifact_manifest_sha256": digest,
              "native_outcome_instance_id": matched.get("instance_id"), "artifact_snapshot_before_evaluation": False,
              "resource_closed": True, "resource_close_scope": "instance_transport", "workspace_captured": True,
              **journal,
              "model_requests": None, "initial_skills": [], "curation_calls": 0, "training_logprobs_available": False,
              "policy_gradient_eligible": False, "started_at": started, "ended_at": time.time(), "wall_seconds": time.time() - started}
    if failure:
        record["failure"] = failure
    return seal(path, record), current, native.get("usage") if threads else previous_usage


def close_family(family, owner):
    errors = []
    try:
        if family.task is not None:
            actor.close_official_task(family, owner)
    except Exception as error:
        errors.append(str(error))
    finally:
        family.hooks.close()
    for container in sorted(family.owned):
        try:
            actor.remove_owned_container(container)
        except Exception as error:
            errors.append(str(error))
    atomic(family.directory / "close-receipt.json", {"scope": "official_family_task", "closed": not errors,
          "complete": not errors, "owned_resources": sorted(family.owned), "errors": errors})
    if errors:
        raise RuntimeError("Official family cleanup failed: " + "; ".join(errors))


def execute_sequence(config, tasks, root):
    """Run under the collection writer and benchmark cwd; resume only between sealed families."""
    validate_stateful_plan(tasks)
    actor.validate_config(config)
    if config.get("backend") != "codex" or config.get("auth_mode", "chatgpt") != "chatgpt":
        raise ValueError("Stateful collection requires the ChatGPT-authenticated Codex backend")
    root = Path(root).resolve()
    immutable(root / "collection.json", canonical({"mode": "stateful", "tasks": tasks, "configuration": config}))
    repository = str(Path(config["repository"]).resolve())
    if repository not in sys.path:
        sys.path.insert(0, repository)
    records = []
    for name, group in groupby(enumerate(tasks, start=1), key=lambda pair: pair[1]["family"]):
        members = list(group)
        directory = root / "families" / name
        if (config.get("min_free_bytes") and not (directory / "intent.json").exists()
                and shutil.disk_usage(root).free < config["min_free_bytes"]):
            # Stop at a resumable family boundary before allocating the native environment.
            result = {"mode": "stateful", "tasks": len(tasks), "completed_tasks": len(records),
                      "complete": False, "records": records, "unstarted_tasks": tasks[len(records):],
                      "stop_reason": "insufficient_free_disk_for_evidence"}
            atomic(root / "collection-result.json", result)
            return result
        intent = {"tasks": [task for _, task in members], "configuration": config}
        try:
            saved = begin(directory, intent)
        except PendingStage as error:
            raise PendingStage("Family " + name + " was interrupted. Resume is supported only between sealed "
                               "families; reconcile the live state and receipts before starting another attempt.") from error
        cases = [root / "cases" / f"{n:03d}-{task['id']}" for n, task in members]
        if saved is not None:
            if not saved["complete"] or not load(directory / "close-receipt.json")["closed"]:
                raise RuntimeError("A family or its resources require reconciliation")
            for path in cases:
                if file_sha(path / "result.json") != saved["case_results"].get(path.name):
                    raise RuntimeError("A sealed case changed: " + path.name)
        else:
            family = Family(name, directory)
            thread = previous_id = None
            previous_usage = {}
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="benchmark") as owner:
                try:
                    owner.submit(family.start, len(members)).result()
                    for (_, task), path in zip(members, cases, strict=True):
                        record, thread, previous_usage = execute_case(
                            config, task, path, family, owner, previous_id, thread, previous_usage)
                        if not record["complete"]:
                            reason = (record.get("failure") or {}).get("message", "incomplete execution or scoring")
                            raise RuntimeError(f"Stateful attempt {task['id']} requires reconciliation: {reason}")
                        previous_id = record["identity"]["attempt_id"]
                        atomic(root / "PROGRESS.json", {"last_task": task["id"], "planned": len(tasks),
                                                       "visited": len(records) + task["canonical_index"] + 1})
                    if len(members) == len(family.task.instances) and not family.terminal:
                        raise RuntimeError("Full native schedule did not terminate")
                    owner.submit(family.evaluate).result()
                except Exception as error:
                    atomic(directory / "failure.json", {"kind": type(error).__name__, "message": str(error)})
                    raise
                finally:
                    close_family(family, owner)
            seal(directory, {"complete": True, "thread_id": thread, "full_native_schedule": family.terminal,
                             "case_results": {p.name: file_sha(p / "result.json") for p in cases}})
        records.extend({"task": task, "output": path.relative_to(root).as_posix(), "complete": True,
                        "initialization_terminal": load(path / "result.json")["initialization_terminal"]}
                       for (_, task), path in zip(members, cases, strict=True))
    result = {"mode": "stateful", "tasks": len(tasks), "completed_tasks": len(records),
              "complete": True, "records": records}
    atomic(root / "collection-result.json", result)
    return result
