"""RSI preparation, planning and explicit benchmark entrypoint. Stdlib only.

Default action is plan. Plan/check do not load the API key, call models,
instantiate a benchmark system, create containers, or execute a task.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import uuid
import time

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def identity(prefix: str) -> str:
    return prefix + "_" + datetime.now(TZ).strftime("%Y%m%dT%H%M%S%z") + "_" + uuid.uuid4().hex[:8]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with filesystem_path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def filesystem_path(path: Path) -> Path:
    """Native Codex export paths exceed Win32's legacy 260-character limit."""
    text = str(path.absolute())
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        text = "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text
    return Path(text)


def checksum_tree(archive: Path) -> dict:
    base = filesystem_path(archive)
    return {p.relative_to(base).as_posix(): digest(p) for p in base.rglob("*") if p.is_file() and not p.is_symlink() and p.name != "checksums.json"}


def command(args: list[str], *, cwd: Path | None = None, timeout: int = 25) -> dict:
    try:
        process = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout)
        return {
            "exit_code": process.returncode,
            "stdout": process.stdout.decode("utf-8", "replace").replace("\x00", "").strip(),
            "stderr": process.stderr.decode("utf-8", "replace").replace("\x00", "").strip(),
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit_code": None, "stdout": "", "stderr": str(exc)}


def linux(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return str(resolved)
    windows = PureWindowsPath(resolved)
    if not windows.drive or not windows.drive.endswith(":"):
        raise ValueError("Use a local drive path for WSL mapping")
    return "/mnt/" + windows.drive[0].lower() + "/" + "/".join(windows.parts[1:])


def configs(name: str) -> tuple[dict, dict]:
    path = Path(name)
    if not path.is_absolute():
        path = ROOT / "config" / path
    project, experiment = load(ROOT / "config/project.json"), load(path)
    source_key = experiment.get("benchmark_source", "benchmark")
    lock = load(ROOT / "config/sources.lock.json")
    if source_key not in lock["sources"]:
        raise ValueError("Unknown locked benchmark source: " + source_key)
    project["paths"]["benchmark"] = lock["sources"][source_key]["path"]
    project["active_benchmark_source"] = source_key
    return project, experiment


def source_checks(project: dict) -> list[dict]:
    lock = load(ROOT / "config/sources.lock.json")
    checks = []
    for name, item in lock["sources"].items():
        path = ROOT / item["path"]
        actual = command(["git", "-C", str(path), "rev-parse", "HEAD"])
        dirty = command(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"])
        matches = actual["exit_code"] == 0 and actual["stdout"] == item["sha"]
        checks.append({"name": name + "_pinned_clean", "passed": matches and dirty["exit_code"] == 0 and not dirty["stdout"], "sha": actual["stdout"]})
        for relative, expected in item.get("files_sha256", {}).items():
            target = path / relative
            checks.append({"name": name + ":" + relative, "passed": target.is_file() and digest(target) == expected})
    return checks


def config_issues(experiment: dict, project: dict) -> list[str]:
    issues = []
    for key in ("runs", "max_workers", "turn_timeout_seconds", "wall_timeout_seconds") + (("max_steps_per_issue",) if experiment.get("task") == "codebase_adaptation" else ()):
        value = experiment.get(key)
        if type(value) is not int or value <= 0:
            issues.append(key + " must be an explicit positive integer")
    if experiment.get("max_workers") != 1:
        issues.append("This initial runner is validated only for max_workers=1")
    if experiment.get("purpose") == "collection":
        limits = experiment.get("resource_limits", {})
        for key in ("actor_memory_mib", "evaluator_memory_mib", "model_memory_mib", "pids_limit", "cpus_per_container"):
            if type(limits.get(key)) is not int or limits[key] <= 0:
                issues.append("Collection requires positive resource_limits." + key)
    if experiment.get("run_mode") != "replicate":
        issues.append("Only fixed-order replicate is prepared; other modes require separate validation")
    allowed_tasks = ("codebase_adaptation", "database_exploration", "sales_prediction", "cohort_studies", "blind_spectrum_monitoring", "exploitable_poker")
    if experiment.get("task") not in allowed_tasks:
        issues.append("Unknown prepared task family")
    if type(experiment.get("seed")) is not int:
        issues.append("seed must be explicitly specified")
    if type(experiment.get("include_baseline")) is not bool:
        issues.append("include_baseline must be explicitly specified")
    system = experiment.get("system", {})
    if system.get("model") != project["api"]["model"]:
        issues.append("Model must match the verified RSI model")
    for key in ("version", "reasoning_effort"):
        if not system.get(key):
            issues.append("system." + key + " is pending")
    if system.get("version") and not re.fullmatch(r"\d+\.\d+\.\d+", system["version"]):
        issues.append("system.version must be an exact numeric CLI release")
    if system.get("reasoning_effort") and system["reasoning_effort"] not in ("none", "minimal", "low", "medium", "high"):
        issues.append("system.reasoning_effort is not accepted by the pinned adapter")
    for key in ("single_conversation", "disable_native_memories", "use_oauth"):
        if type(system.get(key)) is not bool:
            issues.append("system." + key + " must be an explicit boolean")
    if system.get("use_oauth") is not False:
        issues.append("This entrypoint uses the team API credential, use_oauth must be false")
    if not experiment.get("benchmark_sha"):
        issues.append("benchmark_sha is pending")
    if experiment.get("purpose") == "smoke":
        ids = experiment.get("instance_ids")
        if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str):
            issues.append("Initial smoke must have exactly one explicit instance ID")
        if experiment.get("runs") != 1 or experiment.get("include_baseline") is not False:
            issues.append("Initial smoke must be one run without the automatic paired baseline")
        if experiment.get("schedule") != "rsi_smoke_single_instance":
            issues.append("Smoke requires the explicitly derived single-instance named schedule")
    elif experiment.get("purpose") == "collection":
        guard_proof = ROOT / "reports/runtime-guard-check.json"
        if not guard_proof.is_file() or not load(guard_proof).get("passed"):
            issues.append("Collection requires passing container memory and remote-command timeout fault checks")
        callback_proof = ROOT / "reports/bridge-callback-check.json"
        callback_check = load(callback_proof) if callback_proof.is_file() else {}
        if not callback_check.get("passed") or callback_check.get("bridge_sha256") != digest(ROOT / "scripts/clbench_bridge.py"):
            issues.append("Collection requires native callback checks matching the current bridge")
        if experiment.get("resume_from_run"):
            resume_file = ROOT / "reports/native-resume-check.json"
            resume_check = load(resume_file) if resume_file.exists() else {}
            if (not resume_check.get("passed") or resume_check.get("source_run") != experiment["resume_from_run"]
                    or resume_check.get("helper_sha256") != digest(ROOT / "scripts/native_resume.py")):
                issues.append("Resumption requires matching verified action replay and native thread restoration")
        if not experiment.get("collection_authorized"):
            issues.append("Collection scope is not authorized")
        if not experiment.get("schedule"):
            issues.append("Collection schedule must be explicit")
        proof = ROOT / "reports/protocol-review/isolation-check.json"
        if not proof.is_file() or not load(proof).get("passed") or load(proof).get("candidate_sha") != experiment.get("benchmark_sha"):
            issues.append("No matching verified actor-isolation candidate")
        if experiment.get("task") == "codebase_adaptation" and experiment.get("task_params", {}).get("sanitize_actor_workspace") is not True:
            issues.append("Collection requires explicit actor workspace sanitization")
        if experiment.get("task") != "codebase_adaptation":
            resource_file = ROOT / "reports/task-resources/resources.json"
            resources = load(resource_file) if resource_file.is_file() else {}
            if resources.get("source_sha") != experiment.get("benchmark_sha") or resources.get("tasks", {}).get(experiment.get("task"), {}).get("status") != "ready":
                issues.append("Task-specific resource preparation is incomplete")
        acceptance_file = ROOT / "reports/smoke-acceptance.json"
        acceptance = load(acceptance_file) if acceptance_file.is_file() else {}
        if not acceptance.get("accepted") or acceptance.get("benchmark_sha") != experiment.get("benchmark_sha") or not acceptance.get("actor_isolation"):
            issues.append("Collection requires an accepted isolated smoke on the same source")
    elif experiment.get("purpose") == "batch":
        if not experiment.get("schedule"):
            issues.append("Formal named schedule is pending")
        if not experiment.get("formal_parameters_confirmed"):
            issues.append("Formal experiment parameters are not team-confirmed")
        if not experiment.get("delivery_location"):
            issues.append("Formal delivery location is pending")
        receipt = ROOT / "reports/smoke-acceptance.json"
        acceptance = load(receipt) if receipt.is_file() else {}
        accepted_run = acceptance.get("run_id")
        smoke_root = ROOT / project["paths"]["smoke_runs"]
        evidence_ok = False
        if isinstance(accepted_run, str) and Path(accepted_run).name == accepted_run:
            accepted_path = (smoke_root / accepted_run).resolve()
            evidence_ok = (accepted_path / "manifest.json").is_file()
            for field in ("native_verifier_evidence", "tool_return_evidence", "artifact_evidence"):
                relative = acceptance.get(field)
                target = (accepted_path / relative).resolve() if isinstance(relative, str) else None
                evidence_ok = evidence_ok and target is not None and target.is_relative_to(accepted_path) and target.is_file()
        if acceptance.get("accepted") is not True or not evidence_ok:
            issues.append("No accepted end-to-end smoke evidence is recorded")
    else:
        issues.append("purpose must be smoke, collection, or batch")
    if experiment.get("selection_status") != "confirmed":
        issues.append("Episode/schedule selection is a candidate and is not yet confirmed")
    return issues


def preflight(project: dict, experiment: dict) -> dict:
    checks = source_checks(project)
    for name in ("git", "python", "wsl"):
        checks.append({"name": name + "_on_path", "passed": shutil.which(name) is not None})
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    helper = home / project["api"]["credential_helper"]
    checks.append({"name": "encrypted_credential_helper_present", "passed": helper.is_file() and helper.with_name("api-key.dpapi").is_file()})
    if shutil.which("wsl"):
        distro = project["runtime"]["distribution"]
        docker = command(["wsl", "-d", distro, "--", "docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
        checks.append({"name": "docker_server_in_actual_wsl_runtime", "passed": docker["exit_code"] == 0, "detail": docker})
        for image in load(ROOT / "config/images.lock.json")["images"]:
            inspection = command(["wsl", "-d", distro, "--", "docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image["tag"]], timeout=15) if docker["exit_code"] == 0 else {}
            checks.append({"name": "image_digest:" + image["tag"], "passed": inspection.get("exit_code") == 0 and image["digest"] in inspection.get("stdout", ""), "expected_digest": image["digest"]})
        venv = project["runtime"]["venv"] + "/bin/python"
        python = command(["wsl", "-d", distro, "--", venv, "--version"], timeout=20)
        checks.append({"name": "dedicated_linux_venv", "passed": python["exit_code"] == 0, "detail": python})
    issues = config_issues(experiment, project)
    actual_sha = command(["git", "-C", str(ROOT / project["paths"]["benchmark"]), "rev-parse", "HEAD"])
    if experiment.get("benchmark_sha") != actual_sha["stdout"]:
        issues.append("Experiment SHA differs from current locked benchmark checkout")
    return {"checked_at": now(), "purpose": experiment.get("purpose"), "checks": checks, "configuration_issues": issues, "ready_to_execute": all(c["passed"] for c in checks) and not issues, "model_calls": 0, "benchmark_calls": 0}


def make_plan(project: dict, experiment: dict) -> tuple[dict, Path]:
    run_id = identity(experiment.get("purpose", "plan"))
    path = ROOT / project["paths"]["plans"] / run_id
    source = ROOT / project["paths"]["benchmark"]
    purpose = experiment.get("purpose")
    issues = config_issues(experiment, project)
    rows = {}
    is_code = experiment.get("task") == "codebase_adaptation"
    if is_code:
        dataset = source / "data/codebase_adaptation/final-dataset.jsonl"
        for raw in dataset.read_bytes().splitlines(keepends=True):
            if raw.strip():
                item = json.loads(raw)
                rows[item["instance_id"]] = {"instance_id": item["instance_id"], "image": item.get("image_name"), "original_row_sha256": hashlib.sha256(raw).hexdigest()}
    ids = experiment.get("instance_ids") or []
    if purpose in ("batch", "collection") and experiment.get("schedule"):
        spec = load(source / "src/tasks" / experiment["task"] / "schedules" / (experiment["schedule"] + ".json"))
        ids = [i for stage in spec["stages"] for i in stage.get("schedule", {}).get("instance_ids", [])]
    expected = len(ids) if is_code else sum(int(s.get("schedule", {}).get("num_instances", s.get("schedule", {}).get("num_questions", 0))) for s in spec["stages"])
    for item_id in ids:
        if item_id not in rows:
            issues.append("Unknown original instance ID: " + item_id)
    params = {**experiment.get("system", {}), "timeout": experiment.get("turn_timeout_seconds"), "name": "codex_rsi_" + str(purpose)}
    task_params = {"seed": experiment.get("seed")}
    if is_code:
        task_params["max_steps_per_issue"] = experiment.get("max_steps_per_issue")
    if purpose == "smoke":
        task_params.update({"num_instances": 1, "same_repo": True, "schedule": experiment.get("schedule")})
    else:
        task_params["schedule"] = experiment.get("schedule")
    task_params.update(experiment.get("task_params", {}))
    effective = {"system": {"name": "codex", "params": params}, "task": {"name": experiment.get("task"), "params": task_params}, "runs": experiment.get("runs"), "max_workers": 1, "run_mode": experiment.get("run_mode")}
    run_parent = project["paths"]["smoke_runs"] if purpose == "smoke" else project["paths"].get("collection_runs", "runs/collections") if purpose == "collection" else project["paths"]["batch_runs"]
    archive = ROOT / run_parent / run_id
    plan = {"schema_version": 1, "created_at": now(), "run_id": run_id, "purpose": purpose, "benchmark_sha": experiment.get("benchmark_sha"), "api_base_url": project["api"]["base_url"], "requested_model": project["api"]["model"], "instance_ids": ids, "instance_count_per_sequence": expected, "include_baseline": experiment.get("include_baseline"), "wall_timeout_seconds": experiment.get("wall_timeout_seconds"), "effective_config": effective, "archive": str(archive), "linux_archive": linux(archive), "linux_source": linux(source), "linux_runtime": project["runtime"]["work_root"] + "/" + run_id, "issues": issues, "execution_performed": False, "api_calls": 0, "adapter_status": "prepared_not_container_validated", "runner_sha256": digest(Path(__file__)), "bridge_sha256": digest(ROOT / "scripts/clbench_bridge.py")}
    plan.update({"actor_isolation": experiment.get("actor_isolation", "legacy_upstream"), "live_snapshots": purpose == "collection", "source_key": project.get("active_benchmark_source", "benchmark"), "user_authorization": experiment.get("authorization_note")})
    plan["resource_limits"] = experiment.get("resource_limits")
    plan["action_transport"] = experiment.get("action_transport")
    plan["resume_from_run"] = experiment.get("resume_from_run")
    plan["resume_helper_sha256"] = digest(ROOT / "scripts/native_resume.py")
    write(path / "plan.json", plan)
    write(path / "effective-clbench.template.json", effective)
    write(path / "episode-map.json", {"ordered_instances": [rows[i] for i in ids if i in rows], "contains_prompts_or_answers": False})
    write(path / "experiment.snapshot.json", experiment)
    (path / "README.txt").write_text("Planning output only. No model or benchmark was invoked.\nExpected archive: " + str(archive) + "\nUnresolved settings: " + "; ".join(issues) + "\n", encoding="utf-8")
    write(ROOT / "reports" / ("latest-" + str(purpose) + "-plan.json"), {"plan_directory": path.relative_to(ROOT).as_posix(), "run_id": run_id, "execution_performed": False})
    return plan, path


def child_environment(project: dict, *, with_credential: bool) -> dict:
    env = dict(os.environ)
    env["UV_PROJECT_ENVIRONMENT"] = project["runtime"]["venv"]
    names = ["UV_PROJECT_ENVIRONMENT"]
    if with_credential:
        helper = Path(os.environ["USERPROFILE"]) / project["api"]["credential_helper"]
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(helper)], capture_output=True, timeout=15, check=True)
        key = result.stdout.decode().strip()
        if not key:
            raise RuntimeError("Credential helper returned an empty value")
        env["OPENAI_API_KEY"] = key
        env["OPENAI_BASE_URL"] = project["api"]["base_url"]
        names += ["OPENAI_API_KEY", "OPENAI_BASE_URL"]
    existing = [x for x in env.get("WSLENV", "").split(":") if x and x.split("/")[0] not in names]
    env["WSLENV"] = ":".join(existing + [name + "/u" for name in names])
    return env


def prepare_runtime(project: dict) -> int:
    source = ROOT / project["paths"]["benchmark"]
    args = ["wsl", "-d", project["runtime"]["distribution"], "--cd", linux(source), "--", project["runtime"]["uv"], "sync", "--frozen", "--all-extras"]
    log_dir = ROOT / "reports/runtime-setup" / identity("setup")
    log_dir.mkdir(parents=True)
    with (log_dir / "stdout.log").open("wb") as out, (log_dir / "stderr.log").open("wb") as err:
        result = subprocess.run(args, env=child_environment(project, with_credential=False), stdout=out, stderr=err)
    write(log_dir / "result.json", {"checked_at": now(), "exit_code": result.returncode, "command": args, "model_calls": 0, "benchmark_calls": 0})
    print(json.dumps({"exit_code": result.returncode, "evidence": str(log_dir)}, ensure_ascii=False))
    return result.returncode


def prepare_images(project: dict) -> int:
    log_dir = ROOT / "reports/image-setup" / identity("images")
    log_dir.mkdir(parents=True)
    steps = []
    for image in load(ROOT / "config/images.lock.json")["images"]:
        repository = image["tag"].rsplit(":", 1)[0]
        reference = repository + "@" + image["digest"]
        for action in (["pull", reference], ["tag", reference, image["tag"]]):
            result = command(["wsl", "-d", project["runtime"]["distribution"], "--", "docker", *action], timeout=900)
            steps.append({"image": image["tag"], "action": action[0], **result})
            write(log_dir / "result.json", {"steps": steps, "checked_at": now(), "model_calls": 0})
            if result["exit_code"] != 0:
                print(json.dumps({"failed": True, "evidence": str(log_dir)}, ensure_ascii=False))
                return 2
    print(json.dumps({"prepared": True, "evidence": str(log_dir)}, ensure_ascii=False))
    return 0


def existing_collection(plan: dict) -> dict:
    """Avoid duplicate dispatch of matching completed or currently running work."""
    running = []
    phases = {"baseline": set(), "run": set()}
    for run_path in (ROOT / "runs/collections").glob("*"):
        if not (run_path / "control/bridge-plan.json").exists():
            continue
        old = load(run_path / "control/bridge-plan.json")
        if (old.get("benchmark_sha") != plan["benchmark_sha"]
                or old.get("effective_config", {}).get("task") != plan["effective_config"]["task"]
                or old.get("effective_config", {}).get("system") != plan["effective_config"]["system"]):
            continue
        manifest = load(run_path / "manifest.json")
        if manifest["execution_status"] == "running":
            running.append(run_path.name)
        index_path = run_path / "audit/collection-index.json"
        if index_path.exists():
            for episode in load(index_path).get("episodes", []):
                phase = "baseline" if episode["phase"] == "baseline" else "run"
                phases[phase].add((episode["instance_index"], episode["instance_id"]))
    count = plan["instance_count_per_sequence"]
    complete = len(phases["run"]) >= count and (not plan["include_baseline"] or len(phases["baseline"]) >= count)
    return {"running": running, "complete": complete, "graded_by_phase": {key: len(value) for key, value in phases.items()}}


def wait_for_collection_capacity(plan: dict) -> None:
    """Two light scenes may overlap; Docker codebase work gets the VM alone."""
    waiting = False
    while True:
        active = []
        for path in (ROOT / "runs/collections").glob("*/manifest.json"):
            if load(path)["execution_status"] == "running":
                other = load(path.parent / "control/bridge-plan.json")
                active.append(other["effective_config"]["task"]["name"])
        name = plan["effective_config"]["task"]["name"]
        if len(active) < 2 and (not active or (name != "codebase_adaptation" and "codebase_adaptation" not in active)):
            return
        if not waiting:
            print(json.dumps({"waiting_for_capacity": True, "task": name, "active_tasks": active}), flush=True)
            waiting = True
        time.sleep(5)


def run(project: dict, experiment: dict, execute: bool) -> int:
    plan, plan_dir = make_plan(project, experiment)
    if not execute:
        print(json.dumps({"planned": str(plan_dir), "executed": False, "note": "Explicit --execute required for benchmark execution"}, ensure_ascii=False))
        return 0
    if plan["purpose"] == "collection" and not plan["issues"]:
        previous = existing_collection(plan)
        if previous["complete"] or previous["running"]:
            print(json.dumps({"executed": False, "skipped": "already_complete" if previous["complete"] else "already_running", **previous}))
            return 0
        wait_for_collection_capacity(plan)
    report = preflight(project, experiment)
    write(plan_dir / "preflight.json", report)
    write(ROOT / "reports/latest-preflight.json", {"report": (plan_dir / "preflight.json").relative_to(ROOT).as_posix(), "ready_to_execute": report["ready_to_execute"]})
    if not report["ready_to_execute"] or plan["issues"]:
        print(json.dumps({"executed": False, "blocked": True, "evidence": str(plan_dir)}, ensure_ascii=False))
        return 2
    archive = Path(plan["archive"])
    archive.mkdir(parents=True, exist_ok=False)
    (archive / "control").mkdir()
    write(archive / "control/bridge-plan.json", plan)
    write(archive / "control/experiment.json", experiment)
    shutil.copy2(ROOT / "scripts/rsi.py", archive / "control/rsi.snapshot.py")
    shutil.copy2(ROOT / "scripts/clbench_bridge.py", archive / "control/clbench_bridge.snapshot.py")
    shutil.copy2(ROOT / "scripts/native_resume.py", archive / "control/native_resume.py")
    shutil.copy2(ROOT / "config/project.json", archive / "control/project.json")
    shutil.copy2(ROOT / "config/sources.lock.json", archive / "control/sources.lock.json")
    shutil.copy2(ROOT / "config/images.lock.json", archive / "control/images.lock.json")
    args = ["wsl", "-d", project["runtime"]["distribution"], "--cd", plan["linux_source"], "--", "timeout", "--signal=INT", "--kill-after=60s", str(plan["wall_timeout_seconds"]), project["runtime"]["uv"], "run", "--offline", "--frozen", "python", linux(archive / "control/clbench_bridge.snapshot.py"), linux(archive / "control/bridge-plan.json")]
    manifest = load(ROOT / "templates/attempt-manifest.json")
    manifest.update({"run_id": plan["run_id"], "series_id": plan["run_id"], "attempt_id": "attempt_001", "episode_id": plan["instance_ids"][0] if plan["purpose"] == "smoke" else None, "purpose": plan["purpose"], "benchmark_sha": plan["benchmark_sha"], "runner_sha256": plan["runner_sha256"], "started_at": now(), "execution_status": "running", "memory_condition": experiment["system"], "evidence": {"native": "native/", "stdout": "stdout.log", "stderr": "stderr.log", "control": "control/"}})
    manifest["windows_runner_pid"] = os.getpid()
    manifest["image_digest"] = load(ROOT / "config/images.lock.json")["images"]
    manifest["missing_evidence"] = ["Public visible-input hash, exact initial state, session IDs and score fields must be extracted from actual native evidence after the run; original dataset-row hashes are not public-prompt hashes."]
    write(archive / "manifest.json", manifest)
    result_code = None
    error = None
    final_copy_ok = False
    try:
        with (archive / "stdout.log").open("wb") as out, (archive / "stderr.log").open("wb") as err:
            result = subprocess.run(args, env=child_environment(project, with_credential=True), stdout=out, stderr=err)
            result_code = result.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        error = type(exc).__name__  # Do not serialize a process environment or helper output.
    finally:
        # The final logger/destructors may append after bridge.finally's copy.
        seal_args = ["wsl", "-d", project["runtime"]["distribution"], "--", "python3", linux(ROOT / "scripts/seal_native.py"), "--runtime", plan["linux_runtime"], "--archive", plan["linux_archive"]]
        try:
            sealed = subprocess.run(seal_args, env=child_environment(project, with_credential=False), capture_output=True, timeout=600)
            final_copy_ok = sealed.returncode == 0
            (archive / "control/post-exit-copy.stdout.log").write_bytes(sealed.stdout)
            (archive / "control/post-exit-copy.stderr.log").write_bytes(sealed.stderr)
        except (OSError, subprocess.SubprocessError):
            final_copy_ok = False
        manifest.update({"finished_at": now(), "process_exit_code": result_code, "execution_status": "timeout" if result_code in (124, 137) else "process_completed_pending_audit" if result_code == 0 else "infra_or_task_error_pending_audit", "grading_status": "pending_native_evidence_audit", "error_type": error})
        write(archive / "manifest.json", manifest)
        if result_code is not None:
            (archive / "exit-code.txt").write_text(str(result_code) + "\n")
        shutil.copy2(ROOT / "templates/index.csv", archive / "index.csv")
        manifest["archive_status"] = "sealed" if final_copy_ok else "sealed_incomplete_native_copy"
        write(archive / "manifest.json", manifest)
        write(archive / "checksums.json", checksum_tree(archive))
    print(json.dumps({"archive": str(archive), "process_exit_code": result_code, "grading_status": manifest["grading_status"]}, ensure_ascii=False))
    return 1 if result_code is None or not final_copy_ok else result_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "plan", "prepare-runtime", "prepare-images", "run"), nargs="?", default="plan")
    parser.add_argument("--config", default="smoke.json")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    project, experiment = configs(args.config)
    if args.action == "prepare-runtime":
        return prepare_runtime(project)
    if args.action == "prepare-images":
        return prepare_images(project)
    if args.action == "check":
        report = preflight(project, experiment)
        path = ROOT / project["paths"]["preflight"] / (identity("check") + ".json")
        write(path, report)
        write(ROOT / "reports/latest-preflight.json", {"report": path.relative_to(ROOT).as_posix(), "ready_to_execute": report["ready_to_execute"]})
        print(json.dumps({"report": str(path), "ready_to_execute": report["ready_to_execute"], "failed_checks": [c["name"] for c in report["checks"] if not c["passed"]], "configuration_issues": report["configuration_issues"]}, ensure_ascii=False))
        return 0 if report["ready_to_execute"] else 2
    if args.action == "plan":
        plan, path = make_plan(project, experiment)
        print(json.dumps({"plan": str(path), "execution_performed": False, "issues": plan["issues"]}, ensure_ascii=False))
        return 0
    return run(project, experiment, args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
