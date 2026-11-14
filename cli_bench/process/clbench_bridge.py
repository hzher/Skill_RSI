"""Apply router config to the pinned native Codex adapter, then call official CLI.

No benchmark action is performed by importing this module. The recorded single
instance smoke validates native execution. Collection uses the recorded,
independently checked actor-isolation candidate.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import subprocess
import dataclasses
import shlex


def install_runtime_guards(plan: dict, archive: Path, native: Path) -> None:
    """Bound this run's containers and terminate timed-out actor commands inside Docker."""
    policy = plan.get("resource_limits")
    if not policy:
        return
    binary_dir = native / ".rsi-bin"
    binary_dir.mkdir()
    guard = binary_dir / "docker"
    # Log policy only: docker argv may include the provider credential.
    guard.write_text("#!/usr/bin/python3\n" +
        "import os,sys,json\n" +
        "args=sys.argv[1:]\n" +
        f"policy={policy!r}\nrun_id={plan['run_id']!r}\n" +
        "if args and args[0]=='run':\n"
        " name=args[args.index('--name')+1] if '--name' in args else ''\n"
        " role='model' if name.startswith('codex_bench') else 'evaluator' if name.startswith('clbench-generic') else 'actor'\n"
        " memory=str(policy[role+'_memory_mib'])+'m'\n"
        " args[1:1]=['--memory',memory,'--memory-swap',memory,'--pids-limit',str(policy['pids_limit']),'--cpus',str(policy['cpus_per_container']),'--label','rsi.run_id='+run_id]\n"
        "os.execv('/usr/bin/docker',['/usr/bin/docker',*args])\n", encoding="utf-8")
    guard.chmod(0o755)
    os.environ["PATH"] = str(binary_dir) + os.pathsep + os.environ["PATH"]
    from minisweagent.environments.docker import DockerEnvironment
    execute = DockerEnvironment.execute

    def bounded_execute(self, action, cwd="", *, timeout=None):
        seconds = timeout or self.config.timeout
        bounded = dict(action)
        bounded["command"] = (f"timeout --signal=TERM --kill-after=2s {seconds}s bash -lc "
                              + shlex.quote(action.get("command", "")))
        # The original client timeout alone leaves the command running inside Docker.
        return execute(self, bounded, cwd, timeout=seconds + 5)

    DockerEnvironment.execute = bounded_execute
    (archive / "control/resource-policy.json").write_text(json.dumps({
        **policy, "scope": "only containers created by this bridge, labeled with run_id",
        "swap": "disabled per container", "actor_command_timeout": "original timeout, TERM then KILL after 2s; client waits 5s longer",
        "reason": "Prior unbounded actor command exhausted the WSL VM memory and swap",
    }, indent=2), encoding="utf-8")


def install_smoke_schedule(plan: dict) -> dict | None:
    """Expose a one-instance derivative without modifying the official catalog."""
    if plan["purpose"] != "smoke":
        return None
    from src.tasks import schedules
    original_catalog = schedules.list_task_schedules
    parent = schedules.get_task_schedule("codebase_adaptation", "default")
    selected = plan["instance_ids"]
    if len(selected) != 1:
        raise RuntimeError("Smoke schedule must contain exactly one instance")
    stages = []
    for stage in parent.stages:
        matches = [item for item in stage.schedule.get("instance_ids", []) if item in selected]
        if matches:
            stages.append(stage.model_copy(deep=True, update={"schedule": {**stage.schedule, "instance_ids": matches}}))
    if sum(len(stage.schedule["instance_ids"]) for stage in stages) != 1:
        raise RuntimeError("Selected smoke instance is not unique in the official parent schedule")
    schedule_id = plan["effective_config"]["task"]["params"]["schedule"]
    derived = parent.model_copy(deep=True, update={
        "id": schedule_id,
        "display_name": "RSI single-instance wiring check",
        "description": "Explicit one-instance subset of pinned official default; original task, variant and scorer.",
        "stages": stages,
        "run_defaults": schedules.TaskScheduleRunDefaults(runs=1, max_workers=1, mode="replicate"),
    })

    def catalog(family: str):
        official = original_catalog(family)
        return (*official, derived) if family == "codebase_adaptation" else official

    catalog.cache_clear = original_catalog.cache_clear
    schedules.list_task_schedules = catalog
    return {"parent_schedule": "default", "derived_schedule": derived.model_dump(), "original_dataset_modified": False}


def main() -> None:
    control = Path(sys.argv[1]).resolve()
    validate_only = "--validate-only" in sys.argv[2:]
    plan = json.loads(control.read_text(encoding="utf-8-sig"))
    source = Path(plan["linux_source"])
    archive = Path(plan["linux_archive"])
    native = Path(plan["linux_runtime"])
    if validate_only:
        archive = control.parent.parent
        native = native.with_name(native.name + "_validation")
    if native.exists():
        raise RuntimeError("Runtime directory already exists; never overwrite attempts")
    native.mkdir(parents=True)
    native.joinpath("data").symlink_to(source / "data", target_is_directory=True)
    sys.path.insert(0, str(source))
    install_runtime_guards(plan, archive, native)
    if not validate_only and os.environ.get("OPENAI_BASE_URL") != plan["api_base_url"]:
        raise RuntimeError("Router URL missing or inconsistent")
    if not validate_only and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("API key not injected")

    derivation = install_smoke_schedule(plan)
    if derivation is not None:
        (archive / "control" / "schedule-derivation.json").write_text(json.dumps(derivation, indent=2), encoding="utf-8")

    from src.registry import register_system
    from src.systems.codex.system import CodexSystem
    resumer = None
    if plan.get("resume_from_run"):
        from native_resume import NativeResume
        resumer = NativeResume(plan, archive)

    if plan.get("actor_isolation") == "pr26_sanitized":
        from src.tasks.codebase_adaptation.task import CodebaseAdaptationTask
        initialize = CodebaseAdaptationTask._initialize_generic_pr_workspace
        extract_patch = CodebaseAdaptationTask._extract_patch

        def audited_actor_initialization(self, instance):
            def git(*arguments, check=True):
                return subprocess.run(["docker", "exec", "-w", "/testbed", self._env.container_id, "git", *arguments], check=check, text=True, capture_output=True, timeout=30)
            expected_tree = git("rev-parse", instance.raw_data["base_commit"] + "^{tree}").stdout.strip()
            initialize(self, instance)
            actual_tree = git("rev-parse", "HEAD^{tree}").stdout.strip()
            count = int(git("rev-list", "--all", "--count").stdout.strip())
            inaccessible = git("cat-file", "-e", instance.raw_data["base_commit"] + "^{commit}", check=False).returncode != 0
            record = {"instance_id": instance.instance_id, "container_id": self._env.container_id, "expected_base_tree": expected_tree, "actual_actor_tree": actual_tree, "base_tree_matches": actual_tree == expected_tree, "git_commit_count": count, "inherited_base_object_inaccessible": inaccessible, "actor_head": git("rev-parse", "HEAD").stdout.strip()}
            if not self.sanitize_actor_workspace or not record["base_tree_matches"] or count != 1 or not inaccessible:
                raise RuntimeError("Actor boundary verification failed before model execution")
            with (archive / "control/actor-boundaries.jsonl").open("a", encoding="utf-8") as output:
                output.write(json.dumps(record) + "\n")
        CodebaseAdaptationTask._initialize_generic_pr_workspace = audited_actor_initialization

        def capture_full_submission(self):
            patch = extract_patch(self)
            instance = self.instances[self.current_issue_idx]
            container = self._env.container_id
            relative = "control/submissions/" + container[:12] + ".patch"
            target = archive / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(patch, encoding="utf-8")
            record = {"instance_id": instance.instance_id, "container_id": container, "path": relative, "sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(), "characters": len(patch), "steps": self.current_steps, "capture": "Full return value of official _extract_patch before the original grader; no truncation"}
            with (archive / "control/submission-map.jsonl").open("a", encoding="utf-8") as output:
                output.write(json.dumps(record) + "\n")
            return patch
        CodebaseAdaptationTask._extract_patch = capture_full_submission

    class RouterConfiguredCodex(CodexSystem):
        # Identical inherited constructor/signature, interaction and grading;
        # only provider configuration and explicitly selected baseline policy.
        supports_baseline = plan["include_baseline"]
        # The pinned upstream can create a spawn pool for a paired baseline
        # even with max_workers=1. Keep this locally configured class inline.
        parallel_safe = False

        def _build_prompt(self, query, *, include_skill=True):
            prompt = super()._build_prompt(query, include_skill=include_skill)
            if plan.get("action_transport") == "external_json":
                prompt = ("BENCHMARK ACTION TRANSPORT: Your final response is an action artifact. "
                          "The external benchmark runner reads your JSON, dispatches the action fields defined by its schema, "
                          "and returns the real observation next turn. These action fields are not native "
                          "Codex tools and do not need to appear in your native tool list. "
                          "Return exactly one JSON object matching the provided response schema.\n\n" + prompt)
            return prompt

        def respond(self, query):
            self._rsi_instance_id = query.instance_id
            self._rsi_instance_index = query.instance_index
            self._record_visible({"type": "query", "instance_id": query.instance_id, "instance_index": query.instance_index, "prompt": query.prompt})
            try:
                resume = getattr(self, "_rsi_resume", None)
                if resume is not None:
                    replayed = resume.replay_response(query)
                    if replayed is not None:
                        self._record_visible({"type": "replayed_response", "instance_id": query.instance_id, "instance_index": query.instance_index, "source_run": plan["resume_from_run"]})
                        return replayed
                    resume.restore_thread(self)
                result = super().respond(query)
                action = result.action
                if hasattr(action, "model_dump"):
                    action = action.model_dump(mode="json")
                self._record_visible({"type": "response", "instance_id": query.instance_id, "instance_index": query.instance_index, "action": action})
                return result
            except Exception as exc:
                self._record_visible({"type": "infrastructure_exception", "instance_id": query.instance_id, "instance_index": query.instance_index, "exception_type": type(exc).__name__})
                raise

        def observe(self, observation, next_query=None):
            resume = getattr(self, "_rsi_resume", None)
            if resume is not None:
                resume.verify_observation(observation)
            values = dataclasses.asdict(observation) if dataclasses.is_dataclass(observation) else {"content": str(observation)}
            self._record_visible({"type": "observation", "instance_id": getattr(self, "_rsi_instance_id", None), "instance_index": getattr(self, "_rsi_instance_index", None), "observation": values})
            return super().observe(observation, next_query)

        def _record_visible(self, record):
            record["system_container_id"] = getattr(self, "_container_id", None)
            with (archive / "control/visible-events.jsonl").open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

        def _docker_exec(self, command, *, timeout=None):
            if command.startswith("codex "):
                # Codex session files must be owned by the host runner so a
                # baseline reset can actually remove sessions and memories.
                count = getattr(self, "_rsi_native_call_count", 0) + 1
                self._rsi_native_call_count = count
                calls = archive / "control/native-calls"
                calls.mkdir(exist_ok=True)
                name = self._container_id[:12] + f"_{count:04d}"
                record = {"instance_id": getattr(self, "_rsi_instance_id", None), "native_command": command, "inference": "exec " in command, "system_container_id": self._container_id}
                (calls / (name + ".request.json")).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                try:
                    result = subprocess.run(["docker", "exec", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/workspace", "-w", "/workspace", self._container_id, "bash", "-lc", command], capture_output=True, text=True, timeout=timeout or self._timeout)
                except subprocess.TimeoutExpired as exc:
                    for value, suffix in [(exc.stdout, ".events.jsonl"), (exc.stderr, ".stderr.log")]:
                        if isinstance(value, bytes):
                            value = value.decode("utf-8", errors="replace")
                        (calls / (name + suffix)).write_text(value or "", encoding="utf-8")
                    (calls / (name + ".timeout.json")).write_text(json.dumps({"timeout_seconds": exc.timeout, "partial_output_saved": True}), encoding="utf-8")
                    raise
                (calls / (name + ".events.jsonl")).write_text(result.stdout, encoding="utf-8")
                (calls / (name + ".stderr.log")).write_text(result.stderr, encoding="utf-8")
                return result
            return super()._docker_exec(command, timeout=timeout)

        def _start_container(self) -> None:
            super()._start_container()
            provider_config = (
                'model_provider = "rsi_router"\n'
                '[model_providers.rsi_router]\n'
                'name = "RSI AffordableRouter"\n'
                f'base_url = {json.dumps(plan["api_base_url"])}\n'
                'env_key = "OPENAI_API_KEY"\n'
                'wire_api = "responses"\n'
                'requires_openai_auth = false\n'
                'supports_websockets = false\n'
                'request_max_retries = 1\n'
            )
            encoded = base64.b64encode(provider_config.encode()).decode("ascii")
            result = self._docker_exec(
                "umask 077; printf '%s' " + encoded
                + " | base64 -d > /workspace/.codex/config.toml"
                + f" && chown {os.getuid()}:{os.getgid()} /workspace/.codex/config.toml /workspace/.codex/auth.json"
                + " && chmod 600 /workspace/.codex/config.toml /workspace/.codex/auth.json",
                timeout=15,
            )
            if result.returncode:
                raise RuntimeError("Could not install router config inside Codex container")

    RouterConfiguredCodex._rsi_resume = resumer
    register_system("codex")(RouterConfiguredCodex)
    config = plan["effective_config"]
    if plan["purpose"] == "smoke":
        from src.tasks.codebase_adaptation.task import CodebaseAdaptationTask
        probe = CodebaseAdaptationTask(**config["task"]["params"])
        if probe.num_instances != 1 or probe._schedule_instance_ids != plan["instance_ids"]:
            raise RuntimeError("Effective task does not select exactly the intended one instance")
        if probe.max_steps_per_issue != config["task"]["params"]["max_steps_per_issue"]:
            raise RuntimeError("Effective step budget differs from the planned budget")
    config_path = archive / "control" / "effective-clbench.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    os.chdir(native)
    sys.argv = [
        "clbench", "run", "--config", str(config_path),
        "--runs", str(config["runs"]), "--max-workers", "1",
        "--run-mode", config["run_mode"],
        "--output", str(native / "summary.json"),
        "--run-group-id", plan["run_id"],
        "--live-dashboard" if plan.get("live_snapshots") else "--no-live-dashboard", "--no-live-server",
    ]
    if validate_only:
        sys.argv.append("--dry-run")
    metadata = {
        "extension": "router config injection into upstream native Codex adapter",
        "upstream_sha": plan["benchmark_sha"],
        "include_baseline": plan["include_baseline"],
        "cli_argv": sys.argv,
        "bridge_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "runtime": str(native),
        "validation_only": validate_only,
        "native_codex_uid": os.getuid(),
        "native_codex_gid": os.getgid(),
        "actor_isolation": plan.get("actor_isolation", "legacy_upstream"),
        "action_transport": plan.get("action_transport"),
        "resume_from_run": plan.get("resume_from_run"),
    }
    (archive / "control" / "adapter.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        from src.cli import main as clbench_main
        clbench_main()
    finally:
        # Copy only native outputs. Do not copy the data symlink, venv, global
        # home, auth.json, or the Codex executor workspace containing credentials.
        target = archive / "native"
        target.mkdir(exist_ok=True)
        for item in native.iterdir():
            if item.name == "data" or item.is_symlink():
                continue
            dest = target / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)


if __name__ == "__main__":
    main()
