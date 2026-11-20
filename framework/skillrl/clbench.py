"""Optional Continual Learning Bench binding for a native MCP agent loop."""
import argparse
from contextlib import chdir
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from unittest.mock import patch

from .environments import DockerEnvironment
from .interactive import ActionValidationError, TaskBroker, TaskServer, native_arguments, native_task_run_args, run_native
from .messages import RecordingBridge
from .storage import atomic, begin, seal


class ResourceCleanupError(RuntimeError):
    """An initialization failure left official task cleanup unconfirmed."""


class CLBenchTask:
    def __init__(self, repository, family, index, network):
        sys.path.insert(0, str(Path(repository).resolve()))
        from src.registry import get_task_class
        self.task = get_task_class(family)(schedule="default")
        self.container_adaptations = []
        try:
            self._reset(family, index, network)
        except Exception:
            container = getattr(getattr(self.task, "_env", None), "container_id", None)
            try:
                try:
                    self.close()
                finally:
                    if container:
                        remove_owned_container(container)
            except Exception as error:
                raise ResourceCleanupError("Failed to close a partially initialized CL-Bench task") from error
            raise

    def _reset(self, family, index, network):
        if family in ("codebase_adaptation", "sales_prediction"):
            from minisweagent.environments.docker import DockerEnvironment as OfficialDockerEnvironment

            def container_factory(**kwargs):
                original = kwargs["run_args"]
                kwargs["run_args"] = native_task_run_args(original, network)
                self.container_adaptations.append({"original_run_args": original, "native_run_args": kwargs["run_args"]})
                return OfficialDockerEnvironment(**kwargs)

            with patch("minisweagent.environments.docker.DockerEnvironment", container_factory):
                self.query = self.task.reset_baseline_instance(index)
        else:
            self.query = self.task.reset_baseline_instance(index)
        if family == "database_exploration" and self.task._schema_drift_enabled and index >= self.task._pre_drift_count:
            if not self.task._drift_applied:
                self.task._swap_to_drifted_db()
                self.task._drift_applied = True
                self.query = self.task.build_current_query()
        if self.task.canonical_instance_index(0) != index or self.task._selected_instance_indices != [index]:
            raise ValueError("Benchmark instance selection differs from the requested index")
        environment = getattr(self.task, "_env", None)
        self.container = getattr(environment, "container_id", None)
        self.cwd = getattr(getattr(environment, "config", None), "cwd", None) or "/workspace"
        self.done = bool((self.query.metadata or {}).get("done"))
        self.actions = 0

    def status(self):
        return {"done": self.done, "cwd": self.cwd, "query": self.query.prompt if self.query and not self.done else None,
                "response_schema": self.query.response_schema.model_json_schema() if self.query and not self.done else None}

    def act(self, action):
        from src.interface import Response
        if self.done:
            raise ActionValidationError("Task is complete; finish the conversation")
        try:
            validated = self.query.response_schema.model_validate(action)
        except ValueError as error:
            raise ActionValidationError(str(error)) from error
        result = self.task.step(Response(action=validated))
        self.actions += 1
        self.query = result.next_query
        self.done = result.done
        return {"done": self.done, "action": validated.model_dump(mode="json"), "observation": asdict(result.observation), "current": self.status()}

    def outcome(self):
        return asdict(self.task.evaluate()) if self.done else None

    def close(self):
        connection = getattr(self.task, "_conn", None)
        if connection:
            connection.close()
            self.task._conn = None
        temporary = getattr(self.task, "_temp_db_path", None)
        if temporary:
            database = Path(temporary)
            if (database.is_symlink() or database.parent.resolve() != Path(tempfile.gettempdir()).resolve()
                    or database.suffix != ".db"):
                raise ValueError("Unexpected native temporary database path")
            database.unlink(missing_ok=True)
            self.task._temp_db_path = None
        cleanup = getattr(self.task, "_cleanup_container", None)
        if cleanup:
            cleanup()
        executor = getattr(self.task, "_executor", None)
        if executor and hasattr(executor, "close"):
            executor.close()


def docker(args, *, timeout=120, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("Task Docker operation failed: " + result.stderr.decode(errors="replace")[-1000:])
    return result


def remove_owned_container(container):
    if not isinstance(container, str) or not re.fullmatch(r"[0-9a-f]{12,64}", container):
        raise ValueError("Expected an official Docker container ID")
    listing = ["ps", "-aq", "--no-trunc", "--filter", "id=" + container]
    found = docker(listing).stdout.decode().split()
    if not found:
        return
    if len(found) != 1 or not re.fullmatch(r"[0-9a-f]{64}", found[0]) or not found[0].startswith(container):
        raise RuntimeError("Ambiguous owned Docker container ID")
    # Native cleanup can finish asynchronously. Verify the final state even if
    # it removed the same container between the listing and this command.
    docker(["rm", "-f", found[0]], check=False)
    if docker(listing).stdout.strip():
        raise RuntimeError("Official task container removal was not confirmed")


def capture_workspace(container, cwd, path):
    """Archive the official working directory before benchmark cleanup removes it."""
    destination = path / "workspace-final"
    result = docker(["cp", container + ":" + cwd + "/.", str(destination)], check=False)
    files = {str(p.relative_to(destination)): {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in destination.rglob("*") if p.is_file() and not p.is_symlink()} if destination.exists() else {}
    receipt = {"exit_code": result.returncode, "files": files, "stderr": result.stderr.decode(errors="replace")}
    atomic(path / "workspace-capture.json", receipt)
    return result.returncode == 0


def execute(config, family, index, output, *, task_id=None):
    backend = config.get("backend", "claude")
    if backend == "codex":
        from .codex_chatgpt import execute as execute_codex
        return execute_codex(config, family, index, output, task_id=task_id)
    if backend != "claude":
        raise ValueError("Unknown native backend")
    path = Path(output).resolve()
    intent = {"family": family, "index": index, "model": config["model"], "protocol": "native-mcp-v1", "configuration": config}
    cached = begin(path, intent)
    if cached is not None:
        return cached
    task = environment = bridge = server = gateway = None
    owner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="benchmark")
    container = None
    runtime = "/tmp/skillrl-native-" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    try:
        network = json.loads(docker(["network", "inspect", config["network"]]).stdout)[0]
        if not network["Internal"]:
            raise ValueError("Native task tools require an internal-only Docker control network")
        task = owner.submit(CLBenchTask, config["repository"], family, index, config["network"]).result()
        atomic(path / "container-adaptations.json", task.container_adaptations)
        atomic(path / "initial-task.json", owner.submit(task.status).result())
        if task.done:
            return seal(path, {**intent, "complete": True, "initialization_terminal": True, "objective": owner.submit(task.outcome).result(), "model_requests": 0})
        if task.container:
            container = task.container
        else:
            workspace = path / "workspace"
            workspace.mkdir()
            environment = DockerEnvironment(workspace, config["cli_image"], network=config["network"], user="0:0", security_profile="platform")
            environment.start()
            container = environment.container
        atomic(path / "environment.json", {"container_id": container, "cwd": task.cwd, "shared_with_benchmark": bool(task.container), "runtime": runtime})
        docker(["exec", container, "mkdir", "-p", runtime + "/home"])
        docker(["cp", config["cli"], container + ":" + runtime + "/claude"])
        docker(["cp", str(Path(__file__).with_name("mcp_stdio.py")), container + ":" + runtime + "/mcp_stdio.py"])
        version = docker(["exec", container, runtime + "/claude", "--version"])
        atomic(path / "cli-version.json", {"version": version.stdout.decode().strip()})
        broker = TaskBroker(task, path / "tools", owner=owner)
        server = TaskServer(broker, config["bridge_bind"])
        tool_port = server.start()
        public_rpc = "http://" + config["bridge_advertise"] + ":" + str(tool_port) + "/" + server.token
        mcp = {"mcpServers": {"benchmark": {"command": "python3", "args": [runtime + "/mcp_stdio.py"], "env": {"BENCHMARK_RPC_URL": public_rpc}}}}
        atomic(path / "mcp.json", mcp)
        docker(["cp", str(path / "mcp.json"), container + ":" + runtime + "/mcp.json"])
        settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 " + runtime + "/mcp_stdio.py --stop", "timeout": 15}]}]}}
        atomic(path / "settings.json", settings)
        docker(["cp", str(path / "settings.json"), container + ":" + runtime + "/settings.json"])
        if config.get("api_key_command"):
            key = subprocess.check_output(config["api_key_command"]).decode().strip()
        elif config.get("api_key_env"):
            key = os.environ[config["api_key_env"]]
        else:
            key = ""
        base_url = config["base_url"]
        if config.get("upstream_protocol") == "chat":
            from .chat_gateway import ChatMessagesGateway
            gateway = ChatMessagesGateway(base_url, key, config["model"], path / "upstream", timeout=config.get("request_timeout", config.get("timeout", 900)))
            gateway_port = gateway.start()
            base_url = f"http://127.0.0.1:{gateway_port}/v1"
            key = "local-chat-gateway"
        bridge = RecordingBridge(base_url, key, config["model"], path / "requests", bind=config["bridge_bind"], timeout=config.get("timeout", 900))
        port = bridge.start()
        api = "http://" + config["bridge_advertise"] + ":" + str(port) + "/" + bridge.token
        args = native_arguments(container, task.cwd, runtime, api, config["model"], config.get("max_turns", 64))
        args[args.index(container):args.index(container)] = ["-e", "BENCHMARK_RPC_URL=" + public_rpc]
        if config.get("request_timeout"):
            args[args.index(container):args.index(container)] = ["-e", "API_TIMEOUT_MS=" + str(config["request_timeout"] * 1000)]
        prompt = (
            "Complete this one task using the native benchmark MCP tools. All task actions execute in the official environment and count toward its own budget. "
            "Use benchmark.status to read the current action schema and benchmark.act to apply an action. Tool results contain the official observation and next schema. "
            "When done is true, finish with a short summary. Keep public rationale fields to brief action justifications. "
            "The initial environment directory is " + task.cwd + ".\n\n" + task.query.prompt
        )
        native = run_native(args, prompt, path / "native", config.get("timeout", 900), lambda: docker(["stop", "-t", "5", container], check=False))
        captured = docker(["cp", container + ":" + runtime + "/home", str(path / "native-home")], check=False)
        atomic(path / "native-home-capture.json", {"exit_code": captured.returncode})
        workspace_saved = capture_workspace(container, task.cwd, path) if task.container else None
        bridge.close()
        bridge = None
        records = [json.loads(p.read_text()) for p in sorted((path / "requests").glob("*/receipt.json"))]
        closed = all(r["complete"] or r["disposition"] in ("gateway_error", "http_rejection", "local_rejection") for r in records)
        if gateway:
            gateway.close()
            gateway = None
            upstream = [json.loads(p.read_text()) for p in sorted((path / "upstream").glob("*/receipt.json"))]
            closed = closed and all(r["complete"] or r["disposition"] in ("http_rejection", "upstream_error") for r in upstream)
        else:
            upstream = records
        complete = native["complete"] and task.done and not broker.failed and closed and captured.returncode == 0 and workspace_saved is not False
        record = {**intent, "complete": complete, "initialization_terminal": False, "native": native,
                  "task_actions": task.actions, "objective": owner.submit(task.outcome).result(), "model_requests": sum(r["forwarded"] for r in upstream),
                  "ingress_attempts": len(records), "all_requests_closed": closed,
                  "workspace_captured": workspace_saved, "initial_skills": [], "curation_calls": 0}
        return seal(path, record)
    except Exception as error:
        atomic(path / "failure.json", {"kind": type(error).__name__, "message": str(error)})
        raise
    finally:
        if server:
            server.close()
        if bridge:
            bridge.close()
        if gateway:
            gateway.close()
        if environment:
            environment.close()
        if task:
            owner.submit(task.close).result()
        owner.shutdown()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--family", required=True)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    config["repository"] = str(Path(config["repository"]).resolve())
    output = args.output.resolve()
    with chdir(config["repository"]):
        result = execute(config, args.family, args.index, output)
    print(json.dumps({"complete": result["complete"], "model_requests": result["model_requests"], "output": str(args.output)}))
    raise SystemExit(0 if result["complete"] else 1)


if __name__ == "__main__":
    main()
