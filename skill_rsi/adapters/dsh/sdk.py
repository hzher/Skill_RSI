"""Native headless DSH CLI using its SDK profile and JSON-RPC protocol."""

# SDK framing and session lifecycle adapted from Skill_RSI_hzh/dsh_system.py (MIT).
import hashlib
import json
import os
import selectors
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from ...evidence.archive import write
from ..base import host_record, probe_command, read_optional, stop_process_group
from ..native_events import mapping


class SDKError(RuntimeError):
    pass


class SDK:
    def __init__(self, process, selector, stdout, session_id):
        self.process = process
        self.selector = selector
        self.stdout = stdout
        self.session_id = session_id
        self.identity = 0
        self.buffer = bytearray()
        self.last_turn = None
        self.final_response = None

    def send(self, row):
        self.process.stdin.write(json.dumps(row, ensure_ascii=False).encode() + b"\n")
        self.process.stdin.flush()

    def receive(self, deadline):
        while b"\n" not in self.buffer:
            seconds = deadline - time.monotonic()
            if seconds <= 0 or not self.selector.select(seconds):
                raise TimeoutError("DSH SDK deadline elapsed")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise SDKError("DSH subprocess closed its RPC output")
            self.stdout.write(chunk)
            self.stdout.flush()
            self.buffer.extend(chunk)
        end = self.buffer.index(b"\n")
        line = self.buffer[:end]
        del self.buffer[:end + 1]
        try:
            return mapping(json.loads(line))
        except (ValueError, UnicodeError):
            return {}

    def request(self, method, params, deadline):
        self.identity += 1
        identity = self.identity
        self.send({"jsonrpc": "2.0", "id": identity, "method": method, "params": params})
        while time.monotonic() < deadline:
            row = self.receive(deadline)
            if "method" not in row and row.get("id") == identity:
                if row.get("error"):
                    raise SDKError(f"DSH SDK rejected {method}")
                return row.get("result")
            if "method" in row and "id" in row:
                self.send({"jsonrpc": "2.0", "id": row["id"], "error": {
                    "code": -32601, "message": "Interactive requests are unavailable in headless mode"}})
            if row.get("method") == "session.event":
                self.event(mapping(row.get("params")))
        raise TimeoutError("DSH SDK deadline elapsed")

    def event(self, params):
        if params.get("sessionId") != self.session_id:
            return
        row = mapping(params.get("event"))
        data = mapping(row.get("data"))
        if row.get("type") == "turn/end":
            self.last_turn = mapping(data.get("reason")).get("kind")
        elif row.get("type") == "assistant/message":
            message = mapping(data.get("message"))
            blocks = message.get("content") or data.get("content") or []
            if isinstance(blocks, list):
                texts = [item["text"] for item in blocks if isinstance(item, dict)
                         and item.get("type") == "text" and isinstance(item.get("text"), str)]
                if texts:
                    self.final_response = "\n".join(texts)

    def close(self):
        process = self.process
        if process.poll() is None:
            try:
                self.request("shutdown", {}, time.monotonic() + 4)
                process.wait(timeout=4)
            except (OSError, SDKError, TimeoutError, subprocess.TimeoutExpired):
                stop_process_group(process)
        while self.selector.select(0):
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            self.stdout.write(chunk)


def _query_policy(project, state, registration, options, session_id, prompt):
    source = options.get("query_source")
    if not isinstance(source, str) or not registration:
        return prompt, []
    from ..bridge import run_hook
    from . import Adapter

    key = uuid4().hex
    try:
        response = run_hook(Adapter(), project, Path(options["state_dir"]), registration, state.parent, {
            "boundary": "before_use", "event_id": key, "call_id": key, "cwd": str(project), "session_id": session_id,
            "tool_name": "managed_query", "tool_input": {"file_path": source},
            "supported_nodes": {"before_use": ["read_origin", "append_context"]},
        })
    except (OSError, RuntimeError, ValueError):
        return prompt, []
    notes = [(row.get("operation_key"), row["text"]) for row in response.get("actions", [])
             if row.get("kind") == "append_context" and isinstance(row.get("text"), str)]
    return "\n\n".join([prompt, *(text for _, text in notes)]), [key for key, _ in notes if key]


def _record_query_delivery(project, state, state_dir, session_id, operations):
    if not operations:
        return
    from ...runtime import Runtime
    from ..bridge import _confirmed

    runtime = Runtime(project, state_dir=state_dir)
    for operation in operations:
        try:
            _confirmed(runtime, state.parent, {
                "host": "dsh", "layer": "dsh_sdk_prompt_acceptance", "session_id": session_id,
                "effective_scope": "current_query", "operation_key": operation,
            })
        except (OSError, RuntimeError, ValueError):
            continue


def run(output, project, prompt, resume, options):
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("DSH SDK capture requires a nonempty prompt")
    timeout = options.get("timeout", 900)
    if type(timeout) not in (int, float) or not 0 < timeout <= 86400:
        raise ValueError("Host timeout must be in (0, 86400] seconds")
    command = options.get("command") or "dsh"
    state = host_record(project, options["state_dir"], "dsh")
    registration = read_optional(state) or {}
    sessions = Path(options.get("session_root") or state.parent / "sessions")
    sessions.mkdir(parents=True, mode=0o700, exist_ok=True)
    session_id = resume or "skill-rsi-" + uuid4().hex
    argv = [command, "--profile", options.get("profile") or registration.get("profile") or "sdk"]
    patches = [
        {"id": "session-persistence-jsonl", "config": {"root": str(sessions), "compression": "none"}},
        {"id": "sdk-jsonrpc-server", "config": {"maxTokensAsSuccess": False}},
    ]
    if registration.get("patch"):
        patch = Path(registration["patch"])
        if patch.parent != state.parent or patch.is_symlink():
            raise ValueError("DSH registration has an unexpected patch path")
        content = patch.read_bytes()
        if hashlib.sha256(content).hexdigest() != registration.get("patch_sha256"):
            raise ValueError("Managed DSH patch changed")
        resource = Path(registration["resource_path"])
        if resource.parent != state.parent or resource.is_symlink():
            raise ValueError("DSH registration has an unexpected resource path")
        modules = [resource / name for name in ("plugin.mjs", "baseline.mjs")]
        if any(module.is_symlink() for module in modules) or hashlib.sha256(
            b"\0".join(module.read_bytes() for module in modules),
        ).hexdigest() != registration.get("resource_digest"):
            raise ValueError("Managed DSH bridge resources changed")
        plugin_patches = json.loads(content)
        plugin_patches[0]["insert"][0]["config"]["captureDirectory"] = str(output / "capture")
        plugin_patches[0]["insert"][0]["config"]["observe"] = bool(registration.get("observe"))
        plugin_patches[0]["insert"][0]["config"]["baselineTools"] = options.get("baseline_tools") is True
        patches.extend(plugin_patches)
    capture_patch = output / "bridge.patch.json"
    write(capture_patch, patches)
    argv.extend([*options.get("args", ()), "--patch", str(capture_patch)])
    env = os.environ.copy()
    if options.get("dsh_home"):
        env["DSH_HOME"] = str(options["dsh_home"])
    initialize = {"cwd": str(project)}
    for key, native in (("provider", "provider"), ("model", "model"), ("max_tokens", "maxTokens"),
                        ("reasoning_effort", "reasoningEffort")):
        if options.get(key) is not None:
            initialize[native] = options[key]
    result = {"run_id": uuid4().hex, "harness": "dsh", "project": str(project), "path": str(output),
              "capture_format": "cli", "resume_from": resume, "session_id": session_id,
              "harness_version": probe_command(command, "--version"), "returncode": None}
    write(output / "launch.json", {**result, "status": "running"})
    deadline = time.monotonic() + timeout
    with (output / "stdout.jsonl").open("xb") as stdout, (output / "stderr.log").open("xb") as stderr:
        os.fchmod(stdout.fileno(), 0o600)
        os.fchmod(stderr.fileno(), 0o600)
        try:
            process = subprocess.Popen(argv, cwd=project, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=stderr, start_new_session=True)
        except OSError as error:
            result.update(status="failed", missing_reason=type(error).__name__)
            write(output / "launch.json", result)
            return result
        with process, selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            sdk = SDK(process, selector, stdout, session_id)
            try:
                sdk.request("initialize", initialize, min(time.monotonic() + 60, deadline))
                chosen, operations = _query_policy(project, state, registration, options, session_id, prompt)
                sdk.request("session/prompt", {"sessionId": session_id, "contentBlocks": [{"type": "text", "text": chosen}]}, deadline)
                _record_query_delivery(project, state, options["state_dir"], session_id, operations)
                sdk.request("session/wait", {"sessionId": session_id}, deadline)
                result["status"] = "completed" if sdk.last_turn == "completed" else "failed" if sdk.last_turn == "error" else "unknown"
            except (TimeoutError, KeyboardInterrupt) as error:
                result.update(status="interrupted", missing_reason=(
                    "host_timeout" if isinstance(error, TimeoutError) else "host_cancelled"))
            except (OSError, SDKError) as error:
                result.update(status="failed", missing_reason=type(error).__name__)
            finally:
                sdk.close()
            result.update(returncode=process.returncode, final_response=sdk.final_response,
                          last_turn_status=sdk.last_turn)
    capture = output / "capture" / "calls.jsonl"
    if capture.is_file():
        totals = {}
        for row in capture.read_text(encoding="utf-8").splitlines():
            try:
                call = mapping(json.loads(row))
            except ValueError:
                continue
            if call.get("kind") != "model_end":
                continue
            for key, value in mapping(call.get("usage")).items():
                if type(value) is int and value >= 0:
                    totals[key] = totals.get(key, 0) + value
        if totals:
            result["usage"] = totals
    write(output / "launch.json", result)
    return result
