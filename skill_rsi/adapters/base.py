"""Host lifecycle and capture contracts."""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from ..evidence.archive import Archive, write
from ..evidence.schema import identifier


def evidence_input(path, options):
    candidate = Path(path).absolute()
    if candidate.is_symlink():
        raise ValueError(f"Evidence selection is a symlink: {candidate}")
    source = candidate.resolve(strict=True)
    root = source if source.is_dir() else source.parent
    archive = (options or {}).get("archive") or Archive(root)
    return archive, archive.path(source)


def probe_command(command, *args):
    executable = shutil.which(os.fspath(command))
    if executable is None:
        return None
    env = os.environ.copy()
    env["PATH"] = str(Path(executable).parent) + os.pathsep + env.get("PATH", "")
    try:
        outcome = subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=8, check=False,
            stdin=subprocess.DEVNULL, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (outcome.stdout.strip() or outcome.stderr.strip() or "available") if outcome.returncode == 0 else None


class HostAdapter:
    version = "1"
    name: str
    command: str
    default_format = "cli"
    nodes: dict[str, tuple[str, ...]] = {}
    hook_boundary = "next_event"
    offline_formats: tuple[str, ...] = ()

    def capabilities(self, session: Mapping | None = None):
        from .wasm import ABI, available

        session = session or {}
        observed = session.get("nodes") or {}
        nodes = {
            boundary: sorted(set(self.nodes[boundary]).intersection(allowed))
            for boundary, allowed in observed.items() if boundary in self.nodes
        }
        wasm = available() is not None
        capabilities = {cap for caps in nodes.values() for cap in caps}
        if wasm:
            capabilities.add("execute_wasm_component")
        return {
            "host": self.name, "adapter_version": self.version,
            "host_version": session.get("host_version"),
            "boundaries": sorted(nodes), "nodes": nodes,
            "capabilities": sorted(capabilities),
            "reload_boundary": self.hook_boundary,
            "proposal_engines": {"wasm": {"abi": ABI, "available": wasm}},
            "reason": None if any(nodes.values()) else "host bridge has not reported a compatible delivery node",
        }

    def probe(self, project=None, state_dir=None, *, command=None):
        project = Path(project or ".").resolve()
        entry = host_record(project, state_dir, self.name) if state_dir is not None else None
        config = read_optional(entry) if entry else None
        command = command or (config or {}).get("command") or self.command
        host_version = probe_command(command, "--version")
        report = None
        current = config and config.get("adapter_version") == self.version and config.get("bridge_python") == sys.executable
        if current:
            reports = [row for path in sorted((entry.parent / "nodes").glob("*.json"))
                       if (row := read_optional(path)) and row.get("adapter_version") == self.version
                       and row.get("registration_id") == config.get("registration_id")
                       and (host_version is None or row.get("host_version") == host_version)]
            if reports:
                report = {"registration_id": config["registration_id"], "adapter_version": self.version,
                          "host_version": reports[0].get("host_version"),
                          "nodes": {node: caps for row in reports for node, caps in row.get("nodes", {}).items()},
                          "boundaries": [row.get("last_event") for row in reports]}
        executable = shutil.which(command)
        gaps = []
        if executable is None:
            gaps.append("native_cli_unavailable")
        elif host_version is None:
            gaps.append("native_cli_version_unavailable")
        if config and not current:
            gaps.append("registration_update_required")
        elif config and not report:
            gaps.append("awaiting_native_callback")
        if os.name != "posix":
            gaps.append("native_registration_platform_unverified")
        return {
            **self.capabilities(report), "host_version": host_version or (report or {}).get("host_version"),
            "project": str(project), "platform": platform.system(), "command": executable,
            "offline_formats": self.offline_formats, "missing_reasons": gaps,
            "configured": bool(config), "loaded": bool(report), "registration": config,
            "last_observation": report,
        }

    def command_line(self, prompt, resume, options):
        raise ValueError(f"{self.name} requires explicit command argv for this capture mode")

    def launch(self, command=None, *, project, state_dir, prompt=None, resume=None, options=None):
        options = dict(options or {})
        project = Path(project).resolve(strict=True)
        if not project.is_dir():
            raise ValueError(f"Expected a project directory: {project}")
        if command is None and not options.get("command"):
            registration = read_optional(host_record(project, state_dir, self.name)) or {}
            options["command"] = registration.get("command", self.command)
        output = options.get("output")
        if output is None:
            from ..tasks import run_task
            return run_task(self.name, project, state_dir, prompt=prompt, resume=resume,
                            options=options, command=command)
        else:
            output = Path(output).resolve()
            output.mkdir(parents=True, mode=0o700, exist_ok=False)
        try:
            return self.run(output, command, project, prompt, resume, {**options, "state_dir": str(Path(state_dir).resolve())})
        except (OSError, ValueError) as error:
            write(output / "launch.json", {"status": "failed", "harness": self.name,
                                          "missing_reason": type(error).__name__})
            raise

    def environment(self, output, options):
        env = os.environ.copy()
        command = options.get("command")
        if command and os.path.isabs(command):
            env["PATH"] = str(Path(command).parent) + os.pathsep + env.get("PATH", "")
        if self.name == "codex" and options.get("python_executable"):
            python = Path(options["python_executable"])
            if not python.is_absolute():
                raise ValueError("Codex Python executable must be an absolute path")
            env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
        if options.get("task_dir"):
            env["SKILL_RSI_TASK_DIR"] = str(options["task_dir"])
        return env

    def run(self, output, command, project, prompt, resume, options):
        argv = self.command_line(prompt, resume, options) if command is None else command
        if isinstance(argv, (str, bytes, os.PathLike)) or not argv or not all(isinstance(arg, str) and arg for arg in argv):
            raise ValueError("Command must be a nonempty argv sequence")
        timeout = options.get("timeout", 900)
        if type(timeout) not in (int, float) or not 0 < timeout <= 86400:
            raise ValueError("Host timeout must be in (0, 86400] seconds")
        result = {"run_id": uuid4().hex, "harness": self.name, "project": str(project),
                  "resume_from": resume, "harness_version": probe_command(argv[0], "--version") if command is None else None,
                  "path": str(output), "returncode": None, "capture_format": self.default_format}
        env = self.environment(output, options)
        write(output / "launch.json", {**result, "status": "running"})
        stdout = output / "stdout.jsonl"
        stderr = output / "stderr.log"
        with stdout.open("xb") as out, stderr.open("xb") as err:
            stdout.chmod(0o600)
            stderr.chmod(0o600)
            try:
                with subprocess.Popen(argv, cwd=project, env=env, stdin=subprocess.DEVNULL,
                                      stdout=out, stderr=err, start_new_session=True) as process:
                    try:
                        from ..tasks import track_child
                        track_child(options, process.pid)
                        returncode = process.wait(timeout=timeout)
                    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                        stop_process_group(process)
                        result.update(status="interrupted", missing_reason=(
                            "host_timeout" if isinstance(error, subprocess.TimeoutExpired) else "host_cancelled"))
                    else:
                        result.update(status="completed" if returncode == 0 else "failed", returncode=returncode)
            except OSError as error:
                result.update(status="failed", missing_reason=type(error).__name__)
        result.update(self.summarize_cli(stdout))
        write(output / "launch.json", result)
        return result

    def summarize_cli(self, stdout):
        return {}

    @staticmethod
    def cli_rows(stdout):
        with stdout.open("rb") as stream:
            for raw in stream:
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(row, dict):
                    yield row

    def observations(self, project, state_dir):
        location = host_record(project, state_dir, self.name).parent / "observations"
        for path in sorted(location.glob("*.json")):
            row = read_optional(path)
            if row:
                yield row

    def deliver(self, action, key):
        return {"operation_key": key, "status": "skipped", "stage": "submission",
                "missing_reason": "delivery requires a live host event"}

    def decode(self, row, origin):
        raise ValueError(f"{self.name} has no decoder for this record")

    def attach(self, project, state_dir, **options):
        from .install import install
        return install(self, project, state_dir, **options)

    def detach(self, project, state_dir, **options):
        from .install import uninstall
        return uninstall(self, project, state_dir, **options)


def stop_process_group(process):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=4)
            return
        except subprocess.TimeoutExpired:
            continue


def host_record(project, state_dir, host):
    return Path(state_dir).resolve() / "hosts" / identifier(str(Path(project).resolve()), host) / "registration.json"


def read_optional(path):
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value
