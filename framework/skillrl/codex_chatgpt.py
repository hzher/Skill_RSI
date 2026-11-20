"""Account-authenticated native Codex backend for the shared CL-Bench collector."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .clbench import ResourceCleanupError, remove_owned_container
from .interactive import TaskBroker, TaskServer
from .storage import atomic, begin, identifier, seal


def validate_config(config):
    # Persist only known public launch settings; secrets are neither input to
    # this backend nor part of an archive's configuration/identity.
    public = {"auth_mode", "backend", "benchmark_commit", "cli", "execution_mode", "framework_base_commit",
              "framework_commit", "host_python", "min_free_bytes", "model", "network", "paired_fresh_run",
              "protocol_commit", "python", "reasoning_effort", "repository", "stop_on_account_capacity",
              "timeout", "wsl_distribution"}
    if set(config) - public or any(isinstance(v, (list, dict)) for v in config.values()):
        raise ValueError("Unsupported Codex setting; use public launch settings and the CLI account store")
    if config.get("auth_mode", "chatgpt") != "chatgpt":
        raise ValueError("This Codex backend requires auth_mode=chatgpt")
    if not isinstance(config.get("model"), str) or not config["model"].strip():
        raise ValueError("Specify the Codex model")
    timeout = config.get("timeout", 900)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Codex timeout must be positive")
    reserve = config.get("min_free_bytes", 0)
    if type(reserve) is not int or reserve < 0:
        raise ValueError("min_free_bytes must be a nonnegative integer")
    if config.get("host_python") and not config.get("wsl_distribution"):
        raise ValueError("Windows host_python requires the owner's wsl_distribution")


class JournalBroker(TaskBroker):
    """Record dispatch order while the shared broker owns retries and side effects."""
    def __init__(self, *args, journal, **kwargs):
        super().__init__(*args, **kwargs)
        self.journal = Path(journal)
        self.audit_lock = threading.Lock()
        self.sequence = 0

    def append(self, value):
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def call(self, operation_id, tool, arguments):
        identifier(operation_id)
        with self.audit_lock:
            self.sequence += 1
            row = {"sequence": self.sequence, "operation_id": operation_id, "tool": tool, "arguments": arguments,
                   "cached_receipt": (self.path / operation_id / "result.json").is_file()}
            self.append({**row, "type": "tool_intent", "timestamp": time.time()})
            try:
                result = super().call(operation_id, tool, arguments)
            except Exception as error:
                self.append({**row, "type": "tool_error", "timestamp": time.time(),
                             "error": {"type": type(error).__name__, "message": str(error)}})
                raise
            self.append({**row, "type": "tool_result", "timestamp": time.time(), "result": result})
            return result


def read_events(path):
    events, invalid = [], []
    if Path(path).is_file():
        with Path(path).open("rb") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("Expected an event object")
                    events.append(event)
                except ValueError:
                    invalid.append(number)
    return events, invalid


def check_journals(path, task_actions):
    """Match each returned MCP call to the serialized owner dispatch, including cached repeats."""
    mcp, invalid_mcp = read_events(path / "mcp.jsonl")
    broker, invalid_broker = read_events(path / "broker.jsonl")
    report = {"journal_complete": False, "journal_invalid_lines": invalid_mcp,
              "broker_invalid_lines": invalid_broker, "mcp_calls": 0}
    if invalid_mcp or invalid_broker or len(broker) % 2:
        return report

    def signature(operation, tool, arguments, result):
        body = [operation, tool, arguments, result]
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).digest()

    delivered, dispatched = Counter(), Counter()
    first = {}
    applied = 0
    try:
        for row in mcp:
            request = row["request"]
            if request["method"] != "tools/call":
                continue
            report["mcp_calls"] += 1
            operation = row["operation_id"]
            if operation != row["session"] + "-" + str(request["id"]) or "error" in row:
                return report
            params = request["params"]
            delivered[signature(operation, params["name"], params.get("arguments", {}), row["result"])] += 1
        for i in range(0, len(broker), 2):
            intent, receipt = broker[i:i + 2]
            seq = i // 2 + 1
            if (intent["type"] != "tool_intent" or receipt["type"] != "tool_result"
                    or intent["sequence"] != seq or receipt["sequence"] != seq):
                return report
            keys = ("operation_id", "tool", "arguments", "cached_receipt")
            if any(intent[k] != receipt[k] for k in keys):
                return report
            operation, tool, args = receipt["operation_id"], receipt["tool"], receipt["arguments"]
            result = receipt["result"]
            if (type(result["isError"]) is not bool or not isinstance(operation, str)
                    or receipt["cached_receipt"] is not (operation in first)):
                return report
            sig = signature(operation, tool, args, result)
            if operation in first and sig != first[operation]:
                return report
            first[operation] = sig
            dispatched[sig] += 1
            if tool == "act" and not receipt["cached_receipt"] and not result["isError"]:
                applied += 1
    except (KeyError, TypeError, ValueError):
        return report
    report["journal_complete"] = delivered == dispatched and applied == task_actions
    return report


def summarize_native(events, exit_code):
    started, returned, unexpected = set(), set(), set()
    terminal, errors = [], []
    for event in events:
        kind = event.get("type")
        item = event.get("item") or {}
        if item.get("type") == "mcp_tool_call":
            if item.get("server") != "benchmark" or item.get("tool") not in ("status", "act"):
                unexpected.add(str(item.get("server")) + "." + str(item.get("tool")))
            if kind == "item.started":
                started.add(item["id"])
            if kind == "item.completed":
                returned.add(item["id"])
        elif item.get("type") not in (None, "agent_message", "reasoning", "error", "todo_list"):
            unexpected.add(item["type"])
        if kind == "turn.completed":
            terminal.append(event)
        elif kind in ("error", "turn.failed"):
            errors.append(event)
    return {"complete": bool(exit_code == 0 and terminal and not errors and not (started - returned) and not unexpected),
            "exit_code": exit_code, "missing_tool_returns": sorted(started - returned),
            "unexpected_tools": sorted(unexpected), "tool_calls": len(returned), "errors": errors,
            "usage": terminal[-1].get("usage") if terminal else None}


def match_outcome(objective, identity):
    """Bind the native instance; an independently selected singleton may use local ordinal zero."""
    outcomes = (objective or {}).get("instance_outcomes", [])
    if len(outcomes) != 1 or identity.get("instance_id") is None:
        return {}, "unmatched"
    outcome = outcomes[0]
    if outcome.get("instance_id") is None:
        return {}, "unmatched"
    query_id, outcome_id = str(identity["instance_id"]), str(outcome["instance_id"])
    canonical, observed = identity.get("canonical_index"), outcome.get("instance_index")
    singleton = "selected_index" in identity and identity["selected_index"] == canonical and observed == 0
    if observed is not None and observed != canonical and not singleton:
        return {}, "unmatched"
    if query_id == outcome_id:
        return outcome, "exact"
    prefix = "sales_prediction:default:years:"
    if (identity.get("family") == "sales_prediction" and identity.get("schedule") == "default"
            and query_id.startswith(prefix) and observed is not None
            and outcome_id == "sales_prediction:years:" + query_id[len(prefix):]):
        return outcome, "official_sales_default_schedule_alias"
    return {}, "unmatched"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def artifact_manifest(path):
    names = ["initial-task.json", "final-task.json", "task-environment.json", "workspace-capture.json",
             "actor/prompt.txt", "actor/native-cli.jsonl", "actor/native-stderr.txt", "actor/final.txt",
             "actor/actor-config.json", "actor/actor-exit.json", "mcp.jsonl", "broker.jsonl", "database-state.json"]
    names.extend(p.relative_to(path).as_posix() for p in sorted((path / "tools").glob("*/*.json")))
    return {name: {"sha256": digest(path / name), "bytes": (path / name).stat().st_size}
            for name in names if (path / name).is_file()}


def task_prompt(status):
    return ("Complete this task using only the benchmark MCP tools in the official environment. "
            "Read the action schema with benchmark.status and apply actions with benchmark.act. "
            "Continue until done=true, then finish with a brief summary. The benchmark directory is "
            + status["cwd"] + ". The actor workspace is only for transport.\n\n" + status["query"])


def windows_path(path):
    return subprocess.check_output(["wslpath", "-w", str(Path(path).resolve())], text=True).strip()


def run_actor(config, case_path, task, owner, prompt, *, family_workspace=None, resume_thread_id=None):
    path = Path(case_path).resolve()
    broker = JournalBroker(task, path / "tools", owner=owner, journal=path / "broker.jsonl")
    server = TaskServer(broker, "127.0.0.1")
    server.server.daemon_threads = False
    port = server.start()
    try:
        with tempfile.TemporaryDirectory(prefix="clbench-mcp-") as directory:
            connection = Path(directory) / "connection"
            connection.write_text(f"http://127.0.0.1:{port}/{server.token}", encoding="utf-8")
            script = Path(__file__).with_name("codex_mcp_stdio.py")
            args = [str(script), str(connection), str(path / "mcp.jsonl")]
            mcp_python = config.get("python", sys.executable)
            host_python = config.get("host_python")
            if host_python:
                mcp = {"command": "wsl.exe", "args": ["-d", config["wsl_distribution"], "--exec", mcp_python, *args]}
                host_path = windows_path
            else:
                mcp = {"command": mcp_python, "args": args}
                host_path = str
            mcp.update(startup_timeout_sec=60, tool_timeout_sec=900, default_tools_approval_mode="approve")
            spec = {"output": host_path(path / "actor"), "cli": config.get("cli", "codex"), "model": config["model"],
                    "reasoning_effort": config.get("reasoning_effort", "low"), "timeout": config.get("timeout", 900),
                    "prompt": prompt, "mcp": mcp, "resume_thread_id": resume_thread_id}
            if family_workspace is not None:
                spec["family_workspace"] = host_path(Path(family_workspace).resolve())
            host_script = host_path(Path(__file__).with_name("codex_host_actor.py"))
            with (path / "host-launch.stdout").open("xb") as out, (path / "host-launch.stderr").open("xb") as err:
                process = subprocess.run([host_python or sys.executable, "-X", "utf8", host_script],
                                         input=json.dumps(spec).encode(), stdout=out, stderr=err)
            receipt_file = path / "actor/actor-exit.json"
            if not receipt_file.is_file():
                raise RuntimeError(f"Host actor exited without a receipt (exit {process.returncode}); see host-launch.stderr")
            receipt = json.loads(receipt_file.read_bytes())
            events, invalid = read_events(path / "actor/native-cli.jsonl")
            native = summarize_native(events, receipt["exit_code"])
            native.update(timeout=receipt["timeout"], invalid_jsonl_lines=invalid, host_exited=True)
            native["complete"] &= not invalid and not receipt["timeout"] and process.returncode == 0
            threads = list(dict.fromkeys(e["thread_id"] for e in events if e.get("type") == "thread.started"))
    finally:
        server.close()
    return native, threads, broker.failed


def close_official_task(task, owner):
    temporary = getattr(task.task, "_temp_db_path", None)
    owner.submit(task.close).result()
    if getattr(task.task, "_conn", None) is not None:
        raise RuntimeError("Official SQLite connection remains open")
    if temporary is not None and Path(temporary).exists():
        raise RuntimeError("Official temporary database remains after cleanup")
    return {"database_connection_closed": True, "temporary_database_removed": True}


def execute(config, family, index, output, *, task_id=None):
    from .clbench import CLBenchTask, capture_workspace
    validate_config(config)
    path = Path(output).resolve()
    task_id = identifier(task_id if task_id is not None else f"{family}-{index:03d}")
    intent = {"family": family, "index": index, "task_id": task_id, "backend": "codex", "auth_mode": "chatgpt",
              "execution_mode": "independent", "protocol": "native-mcp-v1", "configuration": config}
    cached = begin(path, intent)
    if cached is not None:
        return cached
    started = time.time()
    owner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="benchmark")
    task = None
    closure = {"steps": []}
    record = {**intent, "complete": False, "execution_complete": False, "score": None,
              "scoring_status": "unavailable", "model_requests": None, "initial_skills": [], "curation_calls": 0,
              "training_logprobs_available": False, "policy_gradient_eligible": False,
              "started_at": started, "artifact_snapshot_before_evaluation": False}

    def close_step(name, function):
        try:
            closure["steps"].append({"name": name, "complete": True, "detail": function()})
        except Exception as error:
            closure["steps"].append({"name": name, "complete": False, "error": str(error)})

    try:
        task = owner.submit(CLBenchTask, config["repository"], family, index, config["network"]).result()
        # CLBenchTask verifies reset_baseline_instance selected only this canonical index.
        identity = {"family": family, "task_id": task_id, "canonical_index": index, "selected_index": index,
                    "schedule": "default", "instance_id": task.query.instance_id, "attempt_id": uuid.uuid4().hex}
        record["identity"] = identity
        atomic(path / "identity.json", identity)
        initial = owner.submit(task.status).result()
        atomic(path / "initial-task.json", initial)
        atomic(path / "task-environment.json", {"container_id": task.container, "cwd": task.cwd,
               "container_adaptations": task.container_adaptations,
               "actor_location": "windows_host" if config.get("host_python") else "local_host",
               "task_location": "official_linux_environment", "tools": ["benchmark.status", "benchmark.act"]})
        native, threads, broker_error = {"complete": True, "tool_calls": 0, "usage": None, "errors": []}, [], None
        record["initialization_terminal"] = task.done
        if not task.done:
            try:
                native, threads, broker_error = run_actor(config, path, task, owner, task_prompt(initial))
            except Exception as error:
                # The native environment may have accepted a terminal action before
                # its CLI exit receipt was lost. Freeze and score that same attempt.
                record["failure"] = {"type": type(error).__name__, "message": str(error)}
                atomic(path / "failure.json", record["failure"])
                native["complete"] = False
        record.update(native=native, native_thread_ids=threads, task_actions=task.actions, task_done=task.done)
        atomic(path / "final-task.json", owner.submit(task.status).result())
        if task.container:
            saved = capture_workspace(task.container, task.cwd, path)
        else:
            saved = True
            atomic(path / "workspace-capture.json", {"kind": "official_in_memory_task", "filesystem_workspace": False,
                   "public_outputs": ["final-task.json", "tools/", "mcp.jsonl"], "captured": True})
        db = getattr(task.task, "_temp_db_path", None)
        if db and Path(db).is_file():
            atomic(path / "database-state.json", {"sha256": digest(db), "bytes": Path(db).stat().st_size,
                   "snapshot_kind": "state_digest", "full_state_archived": False,
                   "evaluation_mode": "original_live_object_before_close"})
        atomic(path / "artifact-manifest.json", artifact_manifest(path))
        record["artifact_manifest_sha256"] = digest(path / "artifact-manifest.json")
        record["artifact_snapshot_before_evaluation"] = True
        try:
            objective = owner.submit(task.outcome).result() if task.done else None
        except Exception as error:
            objective, outcome, mapping = None, {}, "native_evaluator_failed"
            atomic(path / "scoring-failure.json", {"type": type(error).__name__, "message": str(error)})
            binding = {"verifier_receipt_sha256": None, "evaluation_failure_sha256": digest(path / "scoring-failure.json")}
        else:
            atomic(path / "native-objective.json", objective)
            outcome, mapping = match_outcome(objective, identity)
            binding = {"verifier_receipt_sha256": digest(path / "native-objective.json")}
        score = int(outcome["success"]) if type(outcome.get("success")) is bool else None
        atomic(path / "scoring-link.json", {"identity": identity, "mapping": mapping,
               "query_instance_id": identity["instance_id"], "outcome_instance_id": outcome.get("instance_id"),
               "artifact_manifest_sha256": record["artifact_manifest_sha256"], **binding})
        journal = check_journals(path, task.actions)
        complete = bool(native["complete"] and task.done and not broker_error and journal["journal_complete"])
        record.update(score_binding_version=1, score_mapping=mapping, native_outcome_instance_id=outcome.get("instance_id"),
                      objective=objective, score=score, scoring_status="scored" if score is not None else "unavailable",
                      workspace_captured=saved, **journal, execution_complete=complete,
                      complete=complete and saved and score is not None)
    except Exception as error:
        record["failure"] = {"type": type(error).__name__, "message": str(error)}
        atomic(path / "failure.json", record["failure"])
        if isinstance(error, ResourceCleanupError):
            closure["steps"].append({"name": "initialization_cleanup", "complete": False, "error": str(error)})
    finally:
        if task is not None:
            container = task.container
            close_step("official_task", lambda: close_official_task(task, owner))
            if container:
                close_step("official_container", lambda: remove_owned_container(container))
        close_step("benchmark_thread", owner.shutdown)
        closure["complete"] = all(row["complete"] for row in closure["steps"])
        atomic(path / "close-receipt.json", closure)
    record.update(resource_closed=closure["complete"], ended_at=time.time(), wall_seconds=time.time() - started)
    record["complete"] = record["complete"] and closure["complete"]
    return seal(path, record)
