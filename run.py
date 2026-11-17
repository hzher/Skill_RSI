#!/usr/bin/env python3
"""Run the official CL Bench with a local DeepSeek Harness process."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import gzip
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
BENCH = Path(os.environ.get("CLBENCH_ROOT", ROOT.parent / "continual-learning-bench")).resolve()
sys.path.insert(0, str(BENCH))
BENCH_COMMIT = "5f8c50eb1e84b2eda2ef4faff757dfc812a0ea26"
SOURCE_PATHS = (
    "run.py", "dsh_system.py", "plugins/clbench-submit/index.mjs", "export_results.py",
    "requirements.txt", "environment.yml", "configs/reference_baselines.json",
    "configs/leaderboard_reference.json", "poker_resume.py", "poker_native_resume.py",
)
RECOVERY_POLICY = "dsh-poker-interaction-resume-v3"
RECOVERY_SOURCES = {"dsh_system.py", "run.py", "poker_resume.py", "poker_native_resume.py"}

TASKS = (
    "exploitable_poker", "blind_spectrum_monitoring", "cohort_studies",
    "database_exploration", "codebase_adaptation", "sales_prediction",
)
DOCKER_TASKS = {"codebase_adaptation", "sales_prediction"}


def load_json(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def save_json(path: Path, value) -> None:
    from src.trace_storage import _atomic_write_json

    _atomic_write_json(path, value)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another process is writing {path.parent}") from exc
        yield


def load_config(path: Path) -> dict:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=True)
    config = load_json(path)
    system = config["system"]
    supported = {"model", "provider", "base_url", "reasoning_effort", "max_tokens",
                 "dsh_bin", "patches", "max_steps", "timeout", "pricing", "tools_mode"}
    if system.keys() - supported:
        raise ValueError(f"Unsupported system config fields: {sorted(system.keys() - supported)}")
    system["patches"] = [str((ROOT / item).resolve()) for item in system.get("patches", [])]
    base_url = system.get("base_url") or os.environ.get("DEEPSEEK_BASE_URL")
    if base_url:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or (
            parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
        ):
            raise ValueError("DSH base_url must be an HTTP(S) URL without credentials, query or fragment")
        system["base_url"] = base_url
    if config.get("pricing_reference"):
        config["pricing_provenance"] = load_json(ROOT / config["pricing_reference"])
    if not os.environ.get("DEEPSEEK_API_KEY"):
        credential_path = Path.home() / ".dsh" / ".credentials.yaml"
        if credential_path.exists():
            import yaml

            key = (yaml.safe_load(credential_path.read_text()) or {}).get("refs", {}).get("DEEPSEEK_API_KEY")
            if isinstance(key, str) and key:
                os.environ["DEEPSEEK_API_KEY"] = key
    return config


def benchmark_resources() -> tuple[dict, dict]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "--no-optional-locks", "-C", str(BENCH), *args])

    commit = git("rev-parse", "HEAD").decode().strip()
    if commit != BENCH_COMMIT:
        raise ValueError(f"CLBENCH_ROOT must be on CL-Bench commit {BENCH_COMMIT}; found {commit}")
    dirty = git("status", "--porcelain", "-z", "--untracked-files=all")
    tracked_changes = git("diff", "--name-only", "-z", "HEAD")
    untracked_sources = git("ls-files", "-z", "--others", "--exclude-standard", "--", "src")
    changed = sorted(set((tracked_changes + untracked_sources).decode().split("\0")) - {""})
    checkout = {
        "bench_commit": commit, "bench_dirty": bool(dirty),
        "bench_status_sha256": hashlib.sha256(dirty).hexdigest(),
        "bench_changed_sha256": {
            name: sha256(path) if (path := BENCH / name).is_file() else None
            for name in changed
        },
    }
    resources = set(git("ls-files", "-z", "--", "data").decode().strip("\0").split("\0")) - {""}
    resources.update({"data/database_exploration/products.db", "data/database_exploration/products_drifted.db"})
    files = {}
    for name in sorted(resources):
        path = BENCH / name
        files[name] = {"bytes": path.stat().st_size, "sha256": sha256(path)} if path.is_file() else {"status": "missing"}
    return checkout, files


def use_benchmark_data() -> None:
    directory = ROOT / "data"
    if directory.is_symlink() or directory.exists():
        if directory.resolve() != BENCH / "data":
            raise ValueError(f"{directory} must point to the data of CLBENCH_ROOT")
    else:
        try:
            directory.symlink_to(BENCH / "data", target_is_directory=True)
        except FileExistsError:
            if directory.resolve() != BENCH / "data":
                raise


def result_from_trace(trace: dict):
    from src.interface import EvalMetrics, InstanceOutcome, TaskResult

    raw = trace["result"]
    fields = {f.name for f in dataclasses.fields(InstanceOutcome)}
    return TaskResult(
        metrics=raw["metrics"], summary=raw["summary"],
        eval_metrics=EvalMetrics(**raw["eval_metrics"]),
        instance_outcomes=[InstanceOutcome(**{k: v for k, v in item.items() if k in fields})
                           for item in trace["instance_outcomes"]],
    )


def attempt_root(job: dict) -> Path:
    path = Path(job["path"])
    return path.parent / "attempts" / path.name.removesuffix(".json.gz")


def valid_trace(job: dict, trace: dict) -> bool:
    outcomes = trace.get("instance_outcomes", [])
    expected = 1 if job["phase"] == "baseline" else job["expected_instances"]
    return (
        trace.get("status") == "completed"
        and trace.get("phase") == job["phase"]
        and trace.get("task", {}).get("name") == job["task"]
        and trace.get("task", {}).get("params", {}).get("schedule") == job["task_params"]["schedule"]
        and trace.get("execution", {}).get("run_group_id") == job["group"]
        and trace.get("execution", {}).get("run_index") == job["index"]
        and len(outcomes) == expected
        and len({row["instance_id"] for row in outcomes if row["instance_id"]}) == expected
        and trace.get("result", {}).get("instance_outcomes") == outcomes
    )


def attempt_records(job: dict) -> list[tuple[Path, dict]]:
    records = []
    for path in attempt_root(job).glob("*/manifest.json"):
        try:
            row = load_json(path)
            if row["attempt_id"] == path.parent.name and isinstance(row["created_at"], str) and "status" in row:
                records.append((path, row))
        except (OSError, KeyError, TypeError, ValueError):
            continue
    return sorted(records, key=lambda item: (item[1]["created_at"], item[1]["attempt_id"]))


def completed_checkpoint(job: dict) -> tuple[dict, str] | None:
    batch = Path(job["batch"])
    for manifest, attempt in reversed(attempt_records(job)):
        if attempt.get("status") != "completed" or attempt.get("fingerprint") != job["fingerprint"]:
            continue
        if attempt.get("execution_fingerprint", job["fingerprint"]) not in job["accepted_executions"]:
            continue
        trace_path = manifest.parent / "trace.json.gz"
        if attempt.get("trace_path") != trace_path.relative_to(batch).as_posix():
            continue
        try:
            digest = sha256(trace_path)
            if digest != attempt.get("trace_sha256"):
                continue
            trace = load_json(trace_path)
            identity = trace.get("capture_identity", {})
            if (not valid_trace(job, trace) or identity.get("attempt_id") != attempt["attempt_id"]
                    or identity.get("execution_fingerprint") != attempt.get("execution_fingerprint")):
                continue
            return trace, digest
        except (OSError, EOFError, AttributeError, KeyError, TypeError, ValueError):
            continue
    return None


def publish_checkpoint(path: Path, checkpoint: tuple[dict, str]) -> None:
    trace, digest = checkpoint
    if not path.is_file() or sha256(path) != digest:
        save_json(path, trace)


def poker_resume_source(job: dict, attempts: list[tuple[Path, dict]]) -> tuple | None:
    if job["task"] != "exploitable_poker" or job["phase"] != "run":
        return None
    batch = Path(job["batch"])
    for path, attempt in reversed(attempts):
        live = path.with_name("live.json.gz")
        if attempt["status"] not in {"failed", "interrupted"} or not live.is_file():
            continue
        if attempt.get("fingerprint") != job["fingerprint"] or (
            attempt.get("execution_fingerprint", job["fingerprint"]) not in job["accepted_executions"]
        ):
            raise ValueError(f"Poker resume fingerprint differs: {live}")
        digest = sha256(live)
        if attempt.get("trace_path") != live.relative_to(batch).as_posix() or attempt.get("trace_sha256") != digest:
            raise ValueError(f"Poker resume trace cannot be verified: {live}")
        prefix = load_json(live)
        if prefix.get("interactions"):
            from poker_native_resume import prepare_native_resume
            from poker_resume import replay_poker_prefix

            replayed = replay_poker_prefix(job, prefix)
            native = prepare_native_resume(batch, attempt, prefix)
            source = {
                "attempt_id": attempt["attempt_id"], "run_id": attempt["run_id"],
                "trace_path": live.relative_to(batch).as_posix(), "trace_sha256": digest,
                "instances": len(prefix.get("instance_outcomes", [])),
                "interactions": len(prefix["interactions"]),
            }
            return prefix, replayed, native, source, attempt["runtime_directories"]
    return None


def execute_job(job: dict) -> str:
    """Run one official baseline instance or rollout under an independent attempt identity."""
    from dsh_system import DSHSystem
    from src.registry import get_task_class
    from src.runs import _run_baseline_instance, run_single

    batch = Path(job["batch"])
    path = Path(job["path"])
    parent = attempt_root(job)
    with exclusive_lock(parent / ".lock"):
        if (checkpoint := completed_checkpoint(job)) is not None:
            publish_checkpoint(path, checkpoint)
            return str(path)
        attempts = attempt_records(job)
        for stale_path, stale in attempts:
            if stale["status"] == "running":
                stale["status"] = "interrupted"
                stale["ended_at"] = None
                save_json(stale_path, stale)
        recovery = poker_resume_source(job, attempts)
        previous_source = recovery[3] if recovery else None
        attempt_id = uuid.uuid4().hex
        identity = {
            "run_id": uuid.uuid4().hex, "attempt_id": attempt_id,
            "job_id": f"{job['task']}/{job['phase']}-{job['index']}",
            "task": job["task"], "phase": job["phase"], "index": job["index"],
            "expected_instance_count": 1 if job["phase"] == "baseline" else job["expected_instances"],
            "fingerprint": job["fingerprint"],
            "execution_fingerprint": job["execution_fingerprint"],
            "mode": "reset_each_instance" if job["phase"] == "baseline" else job["mode"],
            "schedule": job["task_params"]["schedule"],
            "seed": job["seed"], "stage_seeds": job["stage_seeds"],
        }
        if previous_source:
            identity["parent_run_id"] = previous_source["run_id"]
            identity["resume_from"] = previous_source
        directory = parent / attempt_id
        manifest_path = directory / "manifest.json"
        trace_path = directory / "trace.json.gz"
        live = directory / "live.json.gz"
        manifest = {
            "schema_version": 1, **identity, "status": "running", "created_at": now(), "ended_at": None,
            "previous_attempt_id": attempts[-1][1]["attempt_id"] if attempts else None,
            "trace_path": None, "trace_sha256": None,
            "runtime_directories": list(recovery[4]) if recovery else [], "error": None,
            "skill": job["skill_loading"],
        }
        save_json(manifest_path, manifest)
        instances = []

        class JobSystem(DSHSystem):
            def __init__(self, **params):
                super().__init__(**params)
                instances.append(self)

            def reset(self) -> None:
                super().reset()
                manifest["runtime_directories"].append(self.directory.relative_to(batch).as_posix())
                try:
                    save_json(manifest_path, manifest)
                except OSError as exc:
                    self._capture_error("runtime_link", exc)

        trace = None
        try:
            kwargs = dict(
                task_class=get_task_class(job["task"]), task_params=job["task_params"],
                system_class=JobSystem, system_params={**job["system_params"], "capture_identity": identity},
                run_group_id=job["group"], system_name="dsh", task_name=job["task"],
            )
            if job["phase"] == "baseline":
                _, _, trace = _run_baseline_instance(**kwargs, instance_index=job["index"])
            elif recovery:
                from poker_native_resume import restore_native_resume
                from poker_resume import resume_poker

                prefix, replayed, native, source, _ = recovery
                system = JobSystem(**kwargs["system_params"])
                manifest["native_resume"] = restore_native_resume(system, native, prefix)
                save_json(manifest_path, manifest)
                print(f"  resuming {identity['job_id']} after {source['instances']} scored instances "
                      f"and {source['interactions']} accepted interactions", flush=True)
                trace = resume_poker(job, system, prefix, live, replayed=replayed)
            else:
                _, _, trace, _ = run_single(
                    **kwargs, run_index=job["index"], live_trace_path=live,
                )
            trace["capture_identity"] = identity
            trace["system"]["continuity"] = {
                "mode": "native_session", "baseline_scope": "isolated_instance",
                "reset_behavior": "new_session_home_workspace",
            }
            save_json(trace_path, trace)
            if not valid_trace(job, trace):
                raise RuntimeError(f"Incomplete {identity['job_id']}; inspect {directory}")
            manifest["status"] = "completed"
        except BaseException as exc:
            manifest["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            try:
                for system in instances:
                    system.close()
                retained = trace_path if trace_path.exists() else live if live.exists() else None
                if retained:
                    manifest["trace_path"] = retained.relative_to(batch).as_posix()
                    manifest["trace_sha256"] = sha256(retained)
                manifest["ended_at"] = now()
                save_json(manifest_path, manifest)
            except Exception as archive_error:
                if manifest["error"] is None:
                    raise
                print(f"Failed to finalize attempt evidence at {manifest_path}: {archive_error}", file=sys.stderr)
        save_json(path, trace)
        return str(path)


class IncompleteRecoveredJobs(RuntimeError):
    """Known transient failures remain after retrying their independent jobs."""


def execute_with_recovery(job: dict) -> str:
    from dsh_system import RetryableDSHError

    for attempt in range(2):
        try:
            return execute_job(job)
        except Exception as exc:
            cause = exc
            while cause is not None and not isinstance(cause, RetryableDSHError):
                cause = cause.__cause__
            if cause is None:
                raise
            if attempt:
                if cause is exc:
                    raise
                raise RetryableDSHError(str(cause)) from exc
            print(f"  retrying {job['task']}/{job['phase']}-{job['index']} after recoverable DSH response failure", flush=True)


def run_pending_jobs(pending: list[dict], workers: int) -> None:
    from dsh_system import RetryableDSHError

    exhausted = []

    def report(job, result):
        try:
            result()
        except RetryableDSHError:
            exhausted.append(f"{job['phase']}-{job['index']}")
            print(f"  retry exhausted for {job['task']}/{exhausted[-1]}", flush=True)
        else:
            print(f"  completed {job['phase']} {job['index']}", flush=True)

    if workers == 1:
        for job in pending:
            report(job, lambda: execute_with_recovery(job))
    elif pending:
        import multiprocessing

        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            futures = {pool.submit(execute_with_recovery, job): job for job in pending}
            try:
                for future in as_completed(futures):
                    report(futures[future], future.result)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    if exhausted:
        raise IncompleteRecoveredJobs(f"DSH response failures exhausted retries for: {', '.join(sorted(exhausted))}")


def run_task(
    task_name: str, run_dir: Path, profile: str, config: dict, workers: int, provenance: dict,
) -> dict:
    from src.interface import TaskResults
    from src.registry import get_task_class
    from src.runs import RunMode, _merge_baseline_instance_results, derive_run_task_params
    from src.tasks.schedules import get_task_schedule
    from src.trace_metrics import attach_baseline_to_outcomes, build_benchmark_aggregate
    from src.trace_metrics import build_sequence_signature, summarize_instance_outcomes
    from src.trace_storage import BaselineSummaryEntry, RunSummary, RunSummaryRunEntry

    artifact = run_dir / "tasks" / f"{task_name}.json.gz"
    schedule_id = "quick_test" if profile == "quick" else "default"
    schedule = get_task_schedule(task_name, schedule_id)
    task_params = {"schedule": schedule_id}
    task_class = get_task_class(task_name)
    task = task_class(**task_params)
    expected = task.num_instances
    seed = getattr(task, "seed", None)
    stage_seeds = [stage.schedule.get("seed") for stage in schedule.stages]
    defaults = schedule.run_defaults
    runs = 1 if profile == "quick" else defaults.runs
    mode = RunMode(defaults.mode)
    system_params = {**config["system"], "runtime_root": str(run_dir / "native" / task_name)}
    if config.get("skill_loading", {}).get("sha256"):
        system_params.update(
            skill_path=str(run_dir / config["skill_loading"]["path"]),
            skill_sha256=config["skill_loading"]["sha256"],
        )
    group = f"{run_dir.name}-{task_name}"
    checkpoint = run_dir / "checkpoints" / task_name
    checkpoint.mkdir(parents=True, exist_ok=True)
    jobs = []
    common = {
        "task": task_name, "group": group, "system_params": system_params, "batch": str(run_dir),
        **provenance, "expected_instances": expected, "mode": mode.value,
        "skill_loading": config.get("skill_loading"), "seed": seed, "stage_seeds": stage_seeds,
    }
    for index in range(runs):
        jobs.append(dict(
            **common, phase="run", index=index,
            task_params=derive_run_task_params(task_params, mode, index),
            path=str(checkpoint / f"run-{index}.json.gz"),
        ))
    for index in range(expected):
        jobs.append(dict(
            **common, phase="baseline", index=index,
            task_params={**task_params, **(task_class.baseline_task_params or {})},
            path=str(checkpoint / f"baseline-{index}.json.gz"),
        ))
    pending = []
    for job in jobs:
        saved = completed_checkpoint(job)
        if saved is None:
            pending.append(job)
        else:
            publish_checkpoint(Path(job["path"]), saved)
    print(f"{task_name}: {expected} instances × {runs} rollout(s) + baseline; "
          f"{len(pending)} pending jobs, {workers} workers", flush=True)
    run_pending_jobs(pending, workers)

    baseline_traces = [load_json(checkpoint / f"baseline-{index}.json.gz") for index in range(expected)]
    _, baseline, baseline_execution = _merge_baseline_instance_results(
        [(index, result_from_trace(trace), trace) for index, trace in enumerate(baseline_traces)],
        run_group_id=group, system_name="dsh", task_name=task_name, task_params=task_params,
        system_params=system_params, expected_num_instances=expected,
        start_time=min(trace["execution"]["start_time"] for trace in baseline_traces),
        end_time=max(trace["execution"]["end_time"] for trace in baseline_traces),
    )
    traces = [load_json(checkpoint / f"run-{index}.json.gz") for index in range(runs)]
    baseline_outcomes = baseline["instance_outcomes"]
    # Some pinned tasks report position within the active slice or permutation.
    # The official instance ID survives both schedule permutation and slicing.
    expected_identities = {row["instance_id"] for row in baseline_outcomes}
    if len(expected_identities) != expected or any(
        {row["instance_id"] for row in trace["instance_outcomes"]} != expected_identities
        for trace in traces
    ):
        raise RuntimeError(f"Baseline and rollout instance identity sets differ for {task_name}")
    entries = []
    for index, trace in enumerate(traces):
        enriched = attach_baseline_to_outcomes(baseline_outcomes, trace["instance_outcomes"])
        entries.append(RunSummaryRunEntry(
            run_index=index, score=trace["result"]["score"],
            trace_path=f"../checkpoints/{task_name}/run-{index}.json.gz",
            execution=trace["execution"], instance_outcomes=enriched,
            aggregate=summarize_instance_outcomes(enriched),
        ))
    results = TaskResults(group, [result_from_trace(trace) for trace in traces],
                          [trace["execution"] for trace in traces])
    summary = RunSummary(
        run_group_id=group, system=traces[0]["system"],
        task={"name": task_name, "params": {**task_params, "expected_num_instances": expected}},
        schedule=schedule_id, run_count=runs, max_workers=workers, run_mode=mode.value,
        aggregate={**results.aggregate(), **build_benchmark_aggregate(
            baseline_outcomes, [trace["instance_outcomes"] for trace in traces])},
        runs=entries, baseline=BaselineSummaryEntry(
            trace_path="embedded", execution=baseline_execution, instance_outcomes=baseline_outcomes),
        task_brief=baseline.get("task_brief"), sequence_signature=build_sequence_signature(baseline_outcomes),
    )
    payload = {
        "kind": "viewer_artifact", "summary": summary.to_dict(), "baseline_trace": baseline,
        "run_traces": [{"run_index": index, "trace": trace} for index, trace in enumerate(traces)],
    }
    try:
        current = load_json(artifact)
    except (OSError, EOFError, ValueError):
        current = None
    if current != payload:
        save_json(artifact, payload)
        print(f"Saved {artifact}", flush=True)
    return {
        "schedule": schedule_id, "run_mode": mode.value, "rollout_count": runs,
        "instances_per_rollout": expected, "baseline_count": expected,
        "max_workers": workers, "seed": seed, "stage_seeds": stage_seeds,
        "sequence_signature": payload["summary"]["sequence_signature"],
    }


def doctor(config: dict) -> None:
    from dsh_system import DSHSystem

    system = DSHSystem(**config["system"])
    env = {**os.environ, "PATH": str(Path(system.dsh_bin).parent) + os.pathsep + os.environ.get("PATH", "")}
    version = subprocess.run([system.dsh_bin, "--version"], env=env, capture_output=True, text=True, timeout=30)
    print(json.dumps({
        "python": sys.version.split()[0], "python_prefix": sys.prefix,
        "bench_root": str(BENCH), "dsh_bin": system.dsh_bin,
        "dsh_version": version.stdout.strip(), "provider": system.provider, "model": system.model,
        "base_url": system.base_url or "https://api.deepseek.com/anthropic",
        "api_key_available": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "docker": shutil.which("docker"), "pricing": system.pricing,
        "dependencies": {name: bool(importlib.util.find_spec(name)) for name in (
            "litellm", "pydantic", "texasholdem", "datasets", "minisweagent", "lifelines")},
    }, indent=2))
    system.close()


def runtime_provenance(config: dict) -> tuple[dict, dict, dict]:
    from dsh_system import DSHSystem

    checkout, resources = benchmark_resources()
    local = DSHSystem(**config["system"])
    binary = Path(local.dsh_bin)
    env = {**os.environ, "PATH": str(binary.parent) + os.pathsep + os.environ.get("PATH", "")}
    package = binary.resolve().parent.parent
    dependencies = package / "node_modules" / "@deepseek-ai"
    dsh_sources = {}
    for candidate in (package, *(dependencies / name for name in (
        "dsh-llm", "dsh-llm-deepseek", "dsh-llm-deepseek-api-key", "dsh-agent-loop", "dsh-tools",
        "dsh-sdk-jsonrpc-server", "dsh-session-persistence-jsonl",
    ))):
        if (candidate / "package.json").is_file():
            for path in [candidate / "package.json", *sorted((candidate / "lib").glob("*.js"))]:
                dsh_sources[path.relative_to(package).as_posix()] = sha256(path)
    runtime = {
        **checkout, "dsh_bin": str(binary), "dsh_executable_sha256": sha256(binary),
        "dsh_version": subprocess.check_output([str(binary), "--version"], env=env, text=True, timeout=30).strip(),
        "dsh_source_sha256": dsh_sources, "python": sys.version, "bench_root": str(BENCH),
    }
    sources = {name: sha256(ROOT / name) for name in SOURCE_PATHS}
    sources.update({str(path): sha256(path) for path in map(Path, config["system"]["patches"])})
    packages = {
        name: item.version for item in importlib.metadata.distributions() if (name := item.metadata.get("Name"))
    }
    return runtime, resources, {"files": sources, "package_versions": packages}


def authorize_execution_revision(manifest: dict, fingerprint: str, frozen: dict, authorize: bool) -> None:
    if fingerprint == manifest["fingerprint"]:
        return
    revisions = manifest.get("execution_revisions", {})
    if fingerprint in revisions:
        return
    if not authorize:
        raise ValueError("Configuration, source, benchmark assets, or skill changed. Use a new --name.")
    immutable = set(frozen) - {"sources"}
    changed_inputs = sorted(key for key in immutable if manifest.get(key) != frozen[key])
    old_sources, new_sources = manifest["sources"], frozen["sources"]
    changes = {path: {"before": old_sources.get(path), "after": new_sources.get(path)}
               for path in old_sources.keys() | new_sources.keys()
               if old_sources.get(path) != new_sources.get(path)}
    if changed_inputs or not changes or changes.keys() - RECOVERY_SOURCES:
        changed = ", ".join([*changed_inputs, *sorted(changes)])
        raise ValueError(f"Repair resume only permits DSH runner and bridge source changes; changed: {changed}")
    manifest.setdefault("execution_revisions", {})[fingerprint] = {
        "source_changes": changes, "recorded_at": now(), "policy": RECOVERY_POLICY,
    }


def run_batch(args: argparse.Namespace, config: dict, run_dir: Path, tasks: list[str]) -> None:
    from export_results import export_results

    runtime, resources, sources = runtime_provenance(config)
    use_benchmark_data()
    profile = args.command
    comparison = canonical_hash({
        "profile": profile, "system": config["system"], "pricing_provenance": config.get("pricing_provenance"),
        "sources": sources, "runtime": runtime, "resources": resources, "capture_schema_version": 1,
    })
    skill = None
    loading = {"sha256": None, "method": None, "path": None}
    if args.skill:
        skill = args.skill.read_bytes()
        if hashlib.sha256(skill).hexdigest() != args.skill_sha256:
            raise ValueError("Selected SKILL.md does not match --skill-sha256")
        if not skill.decode("utf-8").strip():
            raise ValueError("Selected SKILL.md must contain UTF-8 Markdown")
        loading = {"sha256": args.skill_sha256, "method": "system_prompt_section", "path": "skill/SKILL.md"}
    config["skill_loading"] = loading
    fingerprint = canonical_hash({"comparison_fingerprint": comparison, "skill_sha256": loading["sha256"]})
    frozen = {
        "profile": profile, "system": config["system"], "pricing_provenance": config.get("pricing_provenance"),
        "skill_loading": loading, "runtime": runtime, "sources": sources["files"],
        "benchmark_resources": resources, "package_versions": sources["package_versions"],
    }
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        manifest = load_json(manifest_path)
        authorize_execution_revision(manifest, fingerprint, frozen, args.resume_repair)
    else:
        if args.resume_repair:
            raise ValueError("--resume-repair requires an existing experiment")
        manifest = {
            **frozen,
            "schema_version": 1, "run_name": run_dir.name, "display_name": f"DSH · {config['system']['model']}",
            "harness": "dsh", "fingerprint": fingerprint, "comparison_fingerprint": comparison,
            "training_permission": "evaluation_only", "created_at": now(), "tasks": {},
            "capture": {"schema_version": 1, "request_layer": "native_options", "endpoint_verified": False},
        }
    provenance = {
        "fingerprint": manifest["fingerprint"], "execution_fingerprint": fingerprint,
        "accepted_executions": (manifest["fingerprint"], *manifest.get("execution_revisions", {})),
    }
    if skill is not None:
        frozen = run_dir / loading["path"]
        frozen.parent.mkdir(exist_ok=True)
        if frozen.exists():
            if sha256(frozen) != loading["sha256"]:
                raise ValueError("The frozen skill in the batch directory has changed")
        else:
            pending = frozen.with_suffix(".md.tmp")
            pending.write_bytes(skill)
            pending.replace(frozen)
    for task in tasks:
        manifest["tasks"].setdefault(task, {"status": "pending"})
    save_json(manifest_path, manifest)
    exhausted = []
    for task in tasks:
        manifest["tasks"][task] = {"status": "running"}
        save_json(manifest_path, manifest)
        try:
            workers = args.docker_workers if task in DOCKER_TASKS and args.docker_workers else args.workers
            details = run_task(task, run_dir, profile, config, workers, provenance)
        except IncompleteRecoveredJobs as exc:
            exhausted.append(task)
            manifest["tasks"][task] = {"status": "failed", "error": str(exc), "recovery_exhausted": True}
            save_json(manifest_path, manifest)
            continue
        except BaseException as exc:
            status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            manifest["tasks"][task] = {"status": status, "error": str(exc)}
            save_json(manifest_path, manifest)
            raise
        manifest["tasks"][task] = {"status": "completed", **details}
        save_json(manifest_path, manifest)
    export_profile = "full" if profile == "full" and all(
        manifest["tasks"].get(task, {}).get("status") == "completed" for task in TASKS
    ) else "quick"
    if any((run_dir / "tasks").glob("*.json.gz")):
        exported_profile = export_profile
        rank_unavailable = None
        try:
            export_results(BENCH, run_dir, profile=export_profile)
        except ValueError as exc:
            if export_profile != "full" or not str(exc).endswith("pricing is incomplete"):
                raise
            rank_unavailable = str(exc)
            print(f"Official ranked export unavailable: {rank_unavailable}. Writing raw summaries.", flush=True)
            exported_profile = "quick"
            export_results(BENCH, run_dir, profile=exported_profile)
        manifest["export"] = {"profile": exported_profile, "rank_unavailable": rank_unavailable}
        save_json(manifest_path, manifest)
    if exhausted:
        raise IncompleteRecoveredJobs(f"Unfinished tasks after bounded retries: {', '.join(exhausted)}")
    print(f"Results: {run_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("doctor", "quick", "full", "export"))
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "official.json")
    parser.add_argument("--name", help="Result directory name; reuse it to resume the same configuration")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--docker-workers", type=int, help="Worker limit for Codebase Adaptation and Sales Prediction")
    parser.add_argument("--tasks", nargs="+", choices=TASKS)
    parser.add_argument("--run-dir", type=Path, help="Existing result directory to export")
    parser.add_argument("--skill", type=Path, help="Frozen SKILL.md for a separate validation batch")
    parser.add_argument("--skill-sha256", help="SHA-256 of the approved SKILL.md")
    parser.add_argument("--resume-repair", action="store_true",
                        help="Record a runner/bridge source repair when resuming an existing batch")
    args = parser.parse_args()
    args.config = args.config.resolve()
    if args.run_dir is not None:
        args.run_dir = args.run_dir.resolve()
    if args.skill is not None:
        args.skill = args.skill.resolve()
    os.chdir(ROOT)
    if args.command == "export":
        from export_results import export_results

        if not args.run_dir:
            parser.error("export requires --run-dir")
        export_results(BENCH, args.run_dir.resolve())
        return
    if args.workers < 1 or (args.docker_workers is not None and args.docker_workers < 1):
        parser.error("Worker counts must be positive")
    if (args.skill is None) != (args.skill_sha256 is None):
        parser.error("--skill and --skill-sha256 are required together")
    if args.skill is not None and (not args.name or not re.fullmatch(r"[0-9a-f]{64}", args.skill_sha256)):
        parser.error("--skill requires an explicit --name and lowercase SHA-256")
    config = load_config(args.config.resolve())
    if args.command == "doctor":
        doctor(config)
        return
    if not os.environ.get("DEEPSEEK_API_KEY"):
        parser.error("Set DEEPSEEK_API_KEY or configure the local DSH credential store")
    profile = args.command
    tasks = args.tasks or (["exploitable_poker"] if profile == "quick" else list(TASKS))
    if profile == "quick" and tasks != ["exploitable_poker"]:
        parser.error("quick uses the official Poker quick_test schedule")
    if profile == "full" and set(tasks) & DOCKER_TASKS:
        if not shutil.which("docker"):
            parser.error("Full codebase/sales evaluation requires a running Docker engine and docker on PATH")
    model = re.sub(r"[^a-zA-Z0-9._-]+", "-", config["system"]["model"])
    name = args.name or f"dsh-{model}-{profile}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    if Path(name).name != name or name in {".", ".."}:
        parser.error("--name must be a directory name")
    run_dir = ROOT / "results" / name
    if run_dir.is_symlink():
        parser.error("Result directory cannot be a symlink")
    os.umask(0o077)
    run_dir.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(run_dir / ".lock"):
        run_batch(args, config, run_dir, tasks)


if __name__ == "__main__":
    main()
