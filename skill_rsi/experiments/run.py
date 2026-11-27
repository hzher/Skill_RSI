"""Execute frozen conditions in independent host sessions and score their public outputs."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from time import monotonic
from uuid import uuid4

from ..adapters.registry import get_adapter
from ..components import ComponentRegistry, pack
from ..components._local import state_root
from ..evidence.io import digest, encode, file_lock, identifier, inside, read_json, write_json
from ..evidence.journal import EventJournal
from ..pipelines.evidence import ingest
from ..runtime import Runtime
from .protocol import SCHEMA, number


def benchmark_fingerprint(benchmark):
    return digest(encode(benchmark.configuration()))


def _plain_details(value):
    return {key: value[key] for key in ("status", "reason", "exit_code") if key in value}


def _score_local(benchmark, task_id, workspace, executor, timeout):
    tasks = {task.id: task for task in benchmark.tasks()}
    task = tasks[task_id]
    environment = benchmark.prepare(task, workspace)
    if not hasattr(environment, "metadata"):
        result = executor(task.instruction, timeout)
    else:
        raise ValueError("managed Docker experiments require a benchmark providing run_with_host")
    outcome = benchmark.evaluate(task, workspace, result)
    scorer = "workspace@" + benchmark_fingerprint(benchmark)
    rows = [{"instance_id": task_id, "instance_index": 0, "reward": outcome["score"],
             "success": None if outcome["score"] is None else bool(outcome["score"])}]
    extra = outcome["details"].get("metrics", {})
    if not isinstance(extra, dict) or any(not number(value) for value in extra.values()):
        raise ValueError("native objective metrics must be finite values")
    return {"scorer_version": scorer, "instance_outcomes": rows,
            "details": _plain_details(outcome["details"]), "metrics": extra}


def _score_continual(benchmark, pair, workspace, root, executor, budget, deadline, record):
    workspace.mkdir(mode=0o700)
    session, response = benchmark.reset(pair["task_id"], pair["seed"], root / "benchmark")
    with session:
        transcript = []
        feedback = None
        for step in range(budget["max_steps"]):
            if response["done"]:
                return session.score()
            if monotonic() >= deadline:
                break
            current = response["current"]
            if current is None:
                raise ValueError("official sequence omitted the current query")
            path = inside(workspace, "benchmark-action.json")
            path.unlink(missing_ok=True)
            message = ("Complete the current benchmark step. Write exactly one JSON action to "
                       "benchmark-action.json in the working directory."
                       "\n\n" + current["query"] + "\n\n"
                       + encode({"feedback": feedback, "action_schema": current["response_schema"]}).decode())
            launch = executor(message, deadline - monotonic())
            if launch["status"] != "completed":
                raise RuntimeError("host did not complete the benchmark step")
            response = session.step(read_json(inside(workspace, "benchmark-action.json")))
            feedback = response["observation"]
            transcript.append({"step": step, "visible": current, "accepted_action": response["action"],
                               "feedback": feedback, "sequence_complete": response["done"],
                               "native_run_id": launch["run_id"]})
            write_json(root / "benchmark" / "transcript.json", transcript)
            record("task_action", instance_id=current["instance_id"], instance_index=current["instance_index"],
                   native_run_id=launch["run_id"], action=response["action"])
            record("task_feedback", instance_id=current["instance_id"], instance_index=current["instance_index"],
                   native_run_id=launch["run_id"], feedback=feedback, instance_complete=feedback["instance_complete"])
        if response["done"]:
            return session.score()
    raise RuntimeError("sequence ended at the frozen step or time limit")


def _usage(launches, elapsed):
    usage = {"wall_seconds": elapsed, "host_invocations": len(launches)}
    for key in ("total_tokens", "input_tokens", "output_tokens", "repeated_reads", "rework_steps", "usd"):
        values = [(item.get("usage") or {}).get(key) for item in launches]
        if values and all(number(value) and value >= 0 for value in values):
            usage[key] = sum(values)
    if "total_tokens" not in usage and {"input_tokens", "output_tokens"} <= usage.keys():
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def _condition_plan(registry, condition):
    releases = []
    for item in condition["components"]:
        release = registry.resolve(item["component_id"], item["version"], item["form"])
        if release.digest != item["digest"]:
            raise ValueError("installed release differs from frozen experiment")
        releases.append((release, item["options"]))
    return releases


def _experiment_arm(root, *, protocol, key, pair, condition, benchmark, state_dir, capability_project):
    host = get_adapter(pair["host"])
    attempt = uuid4().hex
    receipt = {"pair_id": key[0], "condition": key[1], "attempt_id": attempt,
               "started_at": datetime.now(timezone.utc).isoformat(), "match": pair,
               "assigned_components": condition["components"], "status": "started", "native_runs": []}
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    write_json(root / "arm.json", receipt)
    workspace = root / "workspace"
    arm_state = root / "runtime-state"
    runtime = None
    settings = dict(protocol["host_options"].get(pair["host"], {}))
    capture_root = root / "capture"
    launches = []
    budget = protocol["budget"]
    start = monotonic()
    deadline = start + budget["wall_seconds"]
    session_id = None
    attached = False
    configured = False
    capabilities = None
    releases = ()

    journal = EventJournal(root / "journal")
    ordinal = 0

    def record(kind, **payload):
        nonlocal ordinal
        ordinal += 1
        journal.append({
            "run_id": attempt, "project_id": attempt, "source_id": "skill-rsi:experiment",
            "event_id": f"{attempt}-{ordinal}", "seq": ordinal, "type": kind,
            "timestamp": datetime.now(timezone.utc).isoformat(), "task_name": pair["task_id"],
            "lineage_id": pair["lineage_id"], **payload,
        })

    record("run_started", evaluation_only=True, assignment={"pair_id": key[0], "condition": key[1], **pair},
           artifact_digests=[item["digest"] for item in condition["components"]])

    def execute(prompt, time_limit):
        nonlocal session_id, attached, configured, runtime
        time_limit = min(time_limit, deadline - monotonic())
        if time_limit <= 0:
            raise RuntimeError("arm wall budget exhausted")
        if not configured and condition["baseline"] != "native":
            runtime = Runtime(workspace, state_dir=arm_state)
            setup = settings.get("setup", {})
            if not (pair['host'] == 'codex' and settings.get('launch', {}).get('api')):
                host.attach(workspace, arm_state, **setup, observe=True,
                            command=settings.get("launch", {}).get("command"))
                attached = True
            configured = True
            for release, options in releases:
                frozen = pack(release.path, root / "artifacts" / (release.digest + ".zip"))
                if frozen.digest != release.digest:
                    raise ValueError("isolated component bytes differ from the frozen release")
                runtime.registry.install(frozen.path)
                runtime.enable(pair["host"], release.component_id, release.version, form=release.form,
                               mode="apply", capabilities=capabilities, options=options)
        options = {**settings.get("launch", {}), "model": pair["model"],
                   "timeout": time_limit, "output": capture_root / f"{len(launches):05d}",
                   "baseline_tools": condition["baseline"] == "model_tools"}
        record("task_query", query=prompt, visibility={"native_record": "observed", "model_input": "unknown"})
        launch = host.launch(project=workspace, state_dir=arm_state, prompt=prompt, resume=session_id, options=options)
        launches.append(launch)
        prior_session = session_id
        session_id = launch.get("session_id") or session_id
        receipt["native_runs"].append({"run_id": launch["run_id"], "status": launch["status"],
                                       "session_id": launch.get("session_id"),
                                       "path": Path(launch["path"]).relative_to(root).as_posix()})
        write_json(root / "arm.json", receipt)
        record("host_turn", native_run_id=launch["run_id"], native_session_id=launch.get("session_id"),
               launch_status=launch["status"], harness_version=launch.get("harness_version"), usage=launch.get("usage"))
        if prior_session and launch.get("session_id") != prior_session:
            raise RuntimeError("native CLI did not confirm continuation of the original session")
        if launch.get("harness_version") != pair["host_version"]:
            raise ValueError("managed host version differs from frozen protocol")
        if launch["status"] != "completed":
            raise RuntimeError("managed host did not complete")
        if hasattr(benchmark, "reset") and not session_id:
            raise RuntimeError("native CLI did not expose a session ID for the continual task")
        if (limit := budget.get("max_tokens")) is not None:
            total = _usage(launches, monotonic() - start).get("total_tokens")
            if total is None or total > limit:
                raise RuntimeError("observed usage cannot satisfy the frozen token limit")
        return launch

    try:
        releases = _condition_plan(ComponentRegistry(state_dir), condition)
        capabilities = host.probe(capability_project, state_dir, command=settings.get("launch", {}).get("command"))
        if capabilities["host_version"] != pair["host_version"]:
            raise ValueError("host capability evidence does not match the pinned version")
        if releases and not capabilities.get("loaded"):
            raise ValueError("the capability project needs a native host callback before assigning treatment")
        if callable(getattr(benchmark, "run_with_host", None)):
            objective = benchmark.run_with_host(pair, workspace, root, execute, budget, deadline)
        elif hasattr(benchmark, "reset"):
            objective = _score_continual(benchmark, pair, workspace, root, execute, budget, deadline, record)
        else:
            objective = _score_local(benchmark, pair["task_id"], workspace, execute, budget["wall_seconds"])
        receipt.update(status="completed", objective=objective)
        for row in objective["instance_outcomes"]:
            common = {key: row[key] for key in ("instance_id", "instance_index")}
            common["scorer_version"] = objective["scorer_version"]
            record("official_score", **common, metric_name="reward", raw_score=row["reward"],
                   grading_status="valid" if number(row["reward"]) else "unscorable", native_success=row.get("success"))
            if row.get("raw_metric_name"):
                value = row.get("raw_metric_value")
                record("official_score", **common, metric_name=row["raw_metric_name"], raw_score=value,
                       grading_status="valid" if number(value) else "unscorable",
                       higher_is_better=row.get("raw_metric_higher_is_better"))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        receipt.update(status="incomplete", reason=str(exc), error_type=type(exc).__name__)
    finally:
        receipt["usage"] = _usage(launches, monotonic() - start)
        receipt["decisions"] = [decision.to_dict() for decision in runtime.decisions(pair["host"])] if runtime else []
        for launch, native in zip(launches, receipt["native_runs"]):
            try:
                captured = ingest(Path(launch["path"]), output=Path(launch["path"]) / "derived" / "skill-rsi-v1",
                                  host=pair["host"], format=launch.get("capture_format"),
                                  options={"evaluation_only": True}, seal=True)
                native["archive"] = Path(captured["archive"]).relative_to(root).as_posix()
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                native["archive_status"] = "unavailable"
                native["missing_reason"] = str(exc)
        for decision in receipt["decisions"]:
            record("operation_receipt", **{name: decision.get(name) for name in (
                "operation_key", "component_id", "version", "binding_revision", "artifact_digest",
                "boundary", "mode", "stage", "status", "reason", "costs", "effective_boundary_ref"
            )}, native_session_id=decision["session_id"], native_run_id=decision.get("run_id"))
        record("run_finished" if receipt["status"] == "completed" else "run_failed",
               status=receipt["status"], reason=receipt.get("reason"), usage=receipt["usage"])
        try:
            archived = journal.publish(root / "evidence", seal=True)
            receipt["evidence"] = Path(archived["archive"]).relative_to(root).as_posix()
        except (OSError, ValueError, RuntimeError) as exc:
            receipt["evidence_missing_reason"] = str(exc)
        if attached:
            try:
                host.detach(workspace, arm_state)
            except (OSError, ValueError, RuntimeError) as exc:
                receipt["detach_error"] = str(exc)
        write_json(root / "arm.json", receipt)
    return receipt


def execute(protocol_path: Path, benchmark, output: Path, *, state_dir=None, capability_project=Path(".")):
    protocol = read_json(protocol_path)
    if protocol.get("schema") != SCHEMA:
        raise ValueError("unknown experiment protocol schema")
    if benchmark_fingerprint(benchmark) != protocol["benchmark_sha256"]:
        raise ValueError("benchmark changed after protocol freeze")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    protocol_sha = digest(protocol_path.read_bytes())
    state_dir = state_root(state_dir)
    with file_lock(output / ".execution.lock"):
        index_path = output / "execution.json"
        index = read_json(index_path) if index_path.exists() else {
            "protocol_sha256": protocol_sha, "status": "running", "arms": []
        }
        if index["protocol_sha256"] != protocol_sha:
            raise ValueError("existing execution uses a different frozen protocol")
        finished = {(arm["pair_id"], arm["condition"]) for arm in index["arms"]}
        write_json(index_path, index)
        for pair_id, pair in protocol["pairs"].items():
            for condition_id, condition in protocol["conditions"].items():
                key = pair_id, condition_id
                if key in finished:
                    continue
                root = output / identifier(pair_id) / identifier(condition_id)
                target = root / "arm.json"
                if target.is_file():
                    arm = read_json(target)
                    if arm["status"] == "started":
                        arm.update(status="incomplete", reason="interrupted_before_terminal_receipt")
                        write_json(target, arm)
                else:
                    if root.exists():
                        if any(root.iterdir()):
                            raise ValueError(f"arm has files without an execution intent: {root}")
                        root.rmdir()
                    arm = _experiment_arm(root, protocol=protocol, key=key, pair=pair,
                                          condition=condition, benchmark=benchmark, state_dir=state_dir,
                                          capability_project=capability_project)
                index["arms"].append({"pair_id": pair_id, "condition": condition_id, "status": arm["status"],
                                      "path": target.relative_to(output).as_posix(), "sha256": digest(target.read_bytes())})
                write_json(index_path, index)
        index["status"] = "completed"
        write_json(index_path, index)
    return index


def metrics(arm):
    rows = arm["objective"]["instance_outcomes"]
    values = {**arm["objective"].get("metrics", {}), **arm.get("usage", {})}
    if rows and all(number(row.get("reward")) for row in rows):
        values["reward"] = mean(row["reward"] for row in rows)
    if rows and all(type(row.get("success")) is bool for row in rows):
        values["success_rate"] = mean(row["success"] for row in rows)
    for name in {row.get("raw_metric_name") for row in rows} - {None}:
        matching = [row for row in rows if row.get("raw_metric_name") == name]
        if len(matching) == len(rows) and all(number(row.get("raw_metric_value")) for row in matching):
            values[name] = mean(row["raw_metric_value"] for row in matching)
    return values
