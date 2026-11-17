"""CL Bench system backed by the official local DSH SDK JSON-RPC profile."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from pydantic import ValidationError
from typing import Any

from src.interface import ContinualLearningSystem, Observation, Query, Response, observation_marks_instance_complete
from src.registry import register_system
from src.usage import UsageEvent


class RetryableDSHError(RuntimeError):
    """A native response failure that allows restarting the benchmark job."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, data: Any) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        pending = Path(handle.name)
        try:
            handle.write(json.dumps(data, ensure_ascii=False).encode())
            handle.close()
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)


@register_system("dsh")
class DSHSystem(ContinualLearningSystem):
    """Keep one native DSH session across task instances and isolate every reset."""

    supports_baseline = True
    parallel_safe = True

    def __init__(
        self,
        model: str = "deepseek-flash",
        provider: str = "deepseek-official",
        base_url: str | None = None,
        reasoning_effort: str = "high",
        max_tokens: int = 16384,
        runtime_root: str | None = None,
        dsh_bin: str | None = None,
        patches: list[str] | None = None,
        max_steps: int = 8,
        timeout: float = 300,
        pricing: dict[str, float] | None = None,
        tools_mode: str = "memory",
        capture_identity: dict[str, Any] | None = None,
        skill_path: str | None = None,
        skill_sha256: str | None = None,
    ):
        if max_tokens < 1 or max_steps < 1 or timeout <= 0:
            raise ValueError("max_tokens, max_steps, and timeout must be positive")
        if tools_mode not in {"memory", "native"}:
            raise ValueError("tools_mode must be memory or native")
        self.model, self.provider = model, provider
        self.base_url, self.reasoning_effort = base_url, reasoning_effort
        self.max_tokens, self.max_steps, self.timeout = max_tokens, max_steps, timeout
        self.pricing = pricing
        self.tools_mode = tools_mode
        self.capture_identity = capture_identity or {}
        self.skill_path = skill_path
        self.skill_sha256 = skill_sha256
        if skill_path is not None:
            skill = Path(skill_path).read_bytes()
            if hashlib.sha256(skill).hexdigest() != skill_sha256:
                raise ValueError("The selected skill file differs from its frozen SHA-256")
            skill.decode("utf-8")
        self.patches = [str(Path(p).expanduser().resolve()) for p in patches or []]
        executable = dsh_bin or os.environ.get("DSH_BIN") or shutil.which("dsh")
        self.dsh_bin = (
            shutil.which(executable) or str(Path(executable).expanduser().absolute())
        ) if executable else None
        if not self.dsh_bin:
            local = Path("/Users/user1/miniconda3/envs/skill-rsi-dsh/bin/dsh")
            if local.is_file():
                self.dsh_bin = str(local)
        if not self.dsh_bin or not os.access(self.dsh_bin, os.X_OK):
            raise FileNotFoundError("DSH executable unavailable. Set DSH_BIN, pass dsh_bin, or add dsh to PATH.")
        self.root = Path(runtime_root).resolve() if runtime_root else None
        self._process: subprocess.Popen | None = None
        self._directories: list[str] = []
        self._feedback: list[str] = []
        self._usage_events: list[UsageEvent] = []
        self._unallocated = True
        if self.root is not None:
            self.reset()

    @property
    def name(self) -> str:
        return f"dsh_{self.model}_{self.reasoning_effort}"

    def reset(self) -> None:
        self._finish_runtime("reset")
        if self.root is None:
            self.root = Path(tempfile.mkdtemp(prefix="clbench-dsh-"))
        self.directory = self.root / uuid.uuid4().hex
        self.workspace = self.directory / "workspace"
        self.home = self.directory / "home"
        self.directory.mkdir(parents=True, mode=0o700)
        self.workspace.mkdir(mode=0o700)
        (self.workspace / "memory").mkdir(mode=0o700)
        self.home.mkdir(mode=0o700)
        self._directories.append(str(self.directory))
        self.session_id = f"clbench-{uuid.uuid4().hex}"
        self.control = self.directory / "query.json"
        self.usage_file = self.directory / "usage.jsonl"
        self.capture = self.directory / "capture"
        self.capture.mkdir(mode=0o700)
        self._usage_offset = 0
        self._feedback.clear()
        self._events: list[dict] = []
        self._buffer = b""
        self._request_id = 0
        self._sequences: dict[str, int] = {}
        self._capture_errors: dict[str, int] = {}
        self._current_query: dict[str, Any] = {}
        self._interaction_step = 0
        self._used = False
        self._closed = False
        self._unallocated = False
        self._record("boundaries", "reset", **self.capture_identity, skill_sha256=self.skill_sha256)

    def _capture_error(self, source: str, exc: BaseException) -> None:
        key = f"{source}:{type(exc).__name__}"
        self._capture_errors[key] = self._capture_errors.get(key, 0) + 1

    def _record(self, log: str, kind: str, **fields: Any) -> None:
        self._sequences[log] = seq = self._sequences.get(log, 0) + 1
        row = {
            "schema_version": 1, "source_seq": seq, "timestamp": _utc(), "kind": kind,
            "session_id": self.session_id, "runtime_id": self.directory.name, **fields,
        }
        try:
            line = json.dumps(row, ensure_ascii=False) + "\n"
            descriptor = os.open(self.capture / f"{log}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(line)
        except (OSError, TypeError, ValueError) as exc:
            self._capture_error(log, exc)

    def _write_health(self, status: str, exit_code: int | None, close_seconds: float) -> None:
        plugin = None
        try:
            if self._used and (self.capture / "plugin_health.json").exists():
                plugin = json.loads((self.capture / "plugin_health.json").read_text(encoding="utf-8"))
            _atomic_json(self.capture / "health.json", {
                "schema_version": 1, "runtime_id": self.directory.name, "session_id": self.session_id,
                "close_status": status, "exit_code": exit_code, "close_seconds": close_seconds,
                "used": self._used, "capture_errors": self._capture_errors,
                "source_counts": self._sequences, "plugin": plugin,
                "recording_closed": status in {"completed", "unused"},
                "flush": "observed" if plugin and plugin.get("disposed") else "unobserved" if self._used else None,
                "durable_prefix_only": self._used and (
                    status != "completed" or exit_code != 0
                    or not (plugin and plugin.get("disposed") and not plugin.get("errors"))
                    or bool(self._capture_errors)
                ),
            })
        except (OSError, ValueError) as exc:
            self._capture_error("health", exc)

    def _start(self) -> None:
        if self._process is not None:
            return
        plugin = Path(__file__).parent / "plugins" / "clbench-submit" / "index.mjs"
        patch = self.directory / "bridge.patch.json"
        persona = (
            "You are evaluating a sequence of tasks in Continual Learning Bench. "
            "Use clbench_submit for every action in the benchmark environment, "
            "including command execution requested by the task. Native filesystem "
            "and shell tools operate in your private scratch workspace and may "
            "support calculations and notes. Treat feedback returned by the "
            "benchmark driver as the authoritative environment observations. "
            "Preserve useful feedback across queries. "
            f"Tool configuration is {self.tools_mode}. Store notes under "
            f"{self.workspace / 'memory'}."
        )
        _atomic_json(patch, [
            {"id": "session-persistence-jsonl", "config": {
                "root": str(self.directory / "sessions"), "compression": "none",
            }},
            {"id": "sdk-jsonrpc-server", "config": {"maxTokensAsSuccess": False}},
            {"id": "system-prompt", "config": {"personaSuffix": persona}},
            {"insert": [{"id": "clbench-submit", "name": plugin.resolve().as_uri(), "config": {
                "controlFile": str(self.control), "usageFile": str(self.usage_file),
                "maxSteps": self.max_steps, "captureDirectory": str(self.capture),
                "runtimeId": self.directory.name,
                "toolsMode": self.tools_mode, "memoryRoot": str(self.workspace / "memory"),
                "skillPath": self.skill_path, "skillSha256": self.skill_sha256,
            }}]},
        ])
        if self.skill_path is not None:
            self._record("boundaries", "skill_configured", sha256=self.skill_sha256,
                         method="system_prompt_section", path="bridge.patch.json")
        command = [self.dsh_bin, "--profile", "sdk"]
        for filename in [*self.patches, str(patch)]:
            command.extend(["--patch", filename])
        env = {**os.environ, "DSH_HOME": str(self.home)}
        env["PATH"] = str(Path(self.dsh_bin).parent) + os.pathsep + env.get("PATH", "")
        if self.base_url:
            env["DEEPSEEK_BASE_URL"] = self.base_url
        self._buffer = b""
        self._stdout = self._stderr = self._selector = None
        try:
            self._stdout = (self.directory / "stdout.jsonl").open("ab")
            self._stderr = (self.directory / "stderr.log").open("ab")
            self._selector = selectors.DefaultSelector()
            self._process = subprocess.Popen(
                command, cwd=self.workspace, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self._stderr, start_new_session=True,
            )
            self._selector.register(self._process.stdout, selectors.EVENT_READ)
            self._request("initialize", {
                "cwd": str(self.workspace), "provider": self.provider, "model": self.model,
                "reasoningEffort": self.reasoning_effort, "maxTokens": self.max_tokens,
            }, time.monotonic() + min(self.timeout, 60))
        except BaseException:
            if self._process is None:
                for stream in (self._selector, self._stdout, self._stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError as exc:
                            self._capture_error("startup_close", exc)
            raise

    def _save_stdout(self, chunk: bytes) -> None:
        try:
            self._stdout.write(chunk)
            self._stdout.flush()
        except OSError as exc:
            self._capture_error("stdout", exc)

    def _request(self, method: str, params: dict, deadline: float) -> Any:
        self._request_id += 1
        identity = self._request_id
        frame = {"jsonrpc": "2.0", "id": identity, "method": method, "params": params}
        self._record("boundaries", "rpc_request", rpc_request_id=identity,
                     query_request_id=self._current_query.get("query_request_id"), method=method, params=params)
        self._process.stdin.write(json.dumps(frame).encode() + b"\n")
        self._process.stdin.flush()
        while True:
            if b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._selector.select(remaining):
                    raise TimeoutError(f"DSH {method} timed out. Runtime logs: {self.directory}")
                chunk = os.read(self._process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError(f"DSH exited during {method}. Runtime logs: {self.directory}")
                self._save_stdout(chunk)
                self._buffer += chunk
                continue
            line, self._buffer = self._buffer.split(b"\n", 1)
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if message.get("method") == "session.event":
                event = message.get("params", {})
                if event.get("sessionId") == self.session_id:
                    self._events.append(event["event"])
            if "method" not in message and message.get("id") == identity:
                if "error" in message:
                    raise RuntimeError(f"DSH {method}: {message['error']}. Logs: {self.directory}")
                return message.get("result")
            if "method" in message and "id" in message:
                # Benchmark runs provide environment feedback through the driver.
                error = {"jsonrpc": "2.0", "id": message["id"], "error": {
                    "code": -32601, "message": "Interactive requests are unavailable in this benchmark",
                }}
                self._process.stdin.write(json.dumps(error).encode() + b"\n")
                self._process.stdin.flush()

    def respond(self, query: Query) -> Response:
        if self._unallocated or self._closed:
            self.reset()
        request_id = uuid.uuid4().hex
        result_file = self.directory / f"action-{request_id}.json"
        queued_feedback = list(self._feedback)
        feedback = queued_feedback.copy()
        if query.feedback and query.feedback.content not in feedback:
            feedback.append(query.feedback.content)
        self._feedback.clear()
        prompt = "\n\n".join([*feedback, query.prompt,
            "Submit the action for this query using clbench_submit. Keep thought fields concise."])
        deadline = time.monotonic() + self.timeout
        self._used = True
        self._interaction_step += 1
        previous_instance = self._current_query.get("instance_id")
        self._current_query = {
            "query_request_id": request_id, "instance_id": query.instance_id,
            "instance_index": query.instance_index, "interaction_step": self._interaction_step,
        }
        if query.instance_id != previous_instance:
            self._record("boundaries", "instance_start", **self._current_query)
        terminal, call_id = None, None
        action_seen = False
        control = {"requestId": request_id, "sessionId": self.session_id, "active": True,
                   "resultFile": str(result_file)}
        try:
            schema = query.response_schema.model_json_schema()
            control["schema"] = schema
            if query.response_schema.__name__ == "PokerAction":
                prompt += " Include the required thinking and action arguments in the same tool call. Keep the string short."
            self._record("queries", "query_start", **self._current_query,
                         prompt=query.prompt, response_schema=schema, query_metadata=query.metadata,
                         query_feedback=asdict(query.feedback) if query.feedback else None,
                         queued_feedback=queued_feedback, sent_prompt=prompt)
            _atomic_json(self.control, control)
            self._start()
            for attempt in range(3):
                if attempt:
                    result_file = self.directory / f"action-{request_id}-repair-{attempt}.json"
                    control["resultFile"] = str(result_file)
                    _atomic_json(self.control, control)
                call_id = None
                self._events.clear()
                self._record("queries", "prompt_sent", **self._current_query,
                             correction_index=attempt, prompt=prompt, action_path=result_file.name)
                self._request("session/prompt", {"sessionId": self.session_id,
                    "contentBlocks": [{"type": "text", "text": prompt}]}, deadline)
                try:
                    self._request("session/wait", {"sessionId": self.session_id}, deadline)
                except TimeoutError as exc:
                    if not result_file.exists() and not self._has_submit_evidence():
                        raise RetryableDSHError(str(exc)) from exc
                    raise
                terminal = next((e["data"].get("reason", {}) for e in reversed(self._events)
                                 if e.get("type") == "turn/end"), {})
                if not result_file.exists():
                    kind = terminal.get("kind")
                    malformed = (
                        kind == "error" and (terminal.get("error") or {}).get("code") == "MALFORMED_RESPONSE"
                        and not action_seen and not self._has_submit_evidence()
                    )
                    if malformed:
                        if attempt == 2 or time.monotonic() >= deadline:
                            raise RetryableDSHError(
                                f"DSH exhausted correction after MALFORMED_RESPONSE. Logs: {self.directory}"
                            )
                        reason = "malformed_response"
                        prefix = "The preceding response could not be decoded. "
                    elif kind in {"completed", "max-tokens"} and attempt < 2:
                        reason = "max_tokens" if kind == "max-tokens" else "no_submission"
                        prefix = "The preceding turn ran out of output tokens. " if kind == "max-tokens" else ""
                    else:
                        raise RuntimeError(f"DSH did not submit an action: {terminal}. Logs: {self.directory}")
                    self._record("queries", "query_correction", **self._current_query,
                                 correction_index=attempt, reason=reason, terminal=terminal)
                    prompt = prefix + (
                        "Use the existing task context and call clbench_submit immediately. "
                        "Include every required field, keep free-text values short, and omit a separate reply."
                    )
                    continue
                action_seen = True
                submission = json.loads(result_file.read_text())
                call_id = submission["toolCallId"]
                self._verify_submission(submission, request_id, terminal)
                try:
                    action = query.response_schema.model_validate(submission["action"])
                except ValidationError as exc:
                    issues = [{"field": ".".join(map(str, item["loc"])), "type": item["type"]}
                              for item in exc.errors(include_input=False, include_context=False, include_url=False)]
                    self._record("queries", "submission_rejected", **self._current_query,
                                 correction_index=attempt, reason="schema_validation", issues=issues,
                                 terminal=terminal, tool_call_id=call_id, action_path=result_file.name)
                    fields = ", ".join(f"{item['field'] or 'action'} ({item['type']})" for item in issues[:12])
                    if attempt == 2:
                        raise RetryableDSHError(f"DSH exhausted schema correction: {fields}. Logs: {self.directory}") from exc
                    prompt = (
                        "The benchmark rejected the previous action before execution. "
                        "Call clbench_submit again with a complete action, correcting these fields: "
                        f"{fields}. Include every required field and keep free-text values short."
                    )
                else:
                    break
            try:
                normalized_action = action.model_dump(mode="json")
            except (TypeError, ValueError) as exc:
                self._capture_error("normalized_action", exc)
                normalized_action = None
            self._record("queries", "query_end", **self._current_query, status="completed",
                         terminal=terminal, tool_call_id=call_id, action_path=result_file.name,
                         normalized_action=normalized_action)
            return Response(action=action, metadata={
                "system_type": "dsh", "model": self.model, "provider": self.provider,
                "reasoning_effort": self.reasoning_effort, "session_id": self.session_id,
                "tools_mode": self.tools_mode,
                "tool_call_id": call_id, "runtime_directory": str(self.directory),
                "query_request_id": request_id, "runtime_id": self.directory.name,
            })
        except BaseException as exc:
            self._record("queries", "query_end", **self._current_query,
                         status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                         terminal=terminal, tool_call_id=call_id,
                         action_path=result_file.name if result_file.exists() else None,
                         error={"type": type(exc).__name__, "message": str(exc)})
            self._stop_process("query_failure")
            raise
        finally:
            try:
                _atomic_json(self.control, {**control, "active": False})
            except (OSError, ValueError) as exc:
                self._capture_error("query_inactivation", exc)
            self._collect_usage()

    def _has_submit_evidence(self) -> bool:
        non_submit_calls = set()
        result_ids = []
        for event in self._events:
            kind, data = event.get("type"), event.get("data") or {}
            if kind in {"tool/call", "tool/ptc-dispatch-start", "tool/ptc-dispatch"}:
                name, call_id = data.get("name"), data.get("callId")
                if not name or name == "clbench_submit":
                    return True
                if call_id:
                    non_submit_calls.add(call_id)
            elif kind == "tool/result":
                result_ids.append((data.get("message") or {}).get("toolCallId"))
            elif kind == "assistant/message":
                content = (data.get("message") or {}).get("content", [])
                if any(block.get("type") == "tool-call" and block.get("name") in {None, "clbench_submit"}
                       for block in content):
                    return True
        return any(call_id not in non_submit_calls for call_id in result_ids)

    def _verify_submission(self, submission: dict, request_id: str, terminal: dict) -> None:
        call_id = submission["toolCallId"]
        calls = [e["data"] for e in self._events if e.get("type") == "tool/call"
                 and e["data"].get("callId") == call_id and e["data"].get("name") == "clbench_submit"]
        results = [e["data"] for e in self._events if e.get("type") == "tool/result"
                   and e["data"].get("message", {}).get("toolCallId") == call_id]
        if (terminal.get("kind") != "completed" or submission["requestId"] != request_id
                or submission.get("sessionId") != self.session_id
                or len(calls) != 1 or len(results) != 1
                or results[0].get("error") or results[0].get("message", {}).get("isError")
                or json.loads(calls[0]["arguments"]) != submission["action"]):
            raise RuntimeError(f"DSH action lacks a committed native tool result. Logs: {self.directory}")

    def _collect_usage(self) -> None:
        try:
            with self.usage_file.open("rb") as stream:
                stream.seek(self._usage_offset)
                for line in stream:
                    if not line.endswith(b"\n"):
                        break
                    self._usage_offset += len(line)
                    if line.strip():
                        try:
                            self._record_usage(json.loads(line))
                        except (AttributeError, KeyError, TypeError, ValueError) as exc:
                            self._capture_error("usage_row", exc)
        except FileNotFoundError:
            return
        except OSError as exc:
            self._capture_error("usage_file", exc)

    def _record_usage(self, row: dict) -> None:
        usage = row.get("usage") or {}
        uncached = usage.get("inputTokens")
        output = usage.get("outputTokens")
        cached = usage.get("cacheReadTokens", 0)
        written = usage.get("cacheWriteTokens", 0)
        input_tokens = None if uncached is None else uncached + cached + written
        total_tokens = usage.get("totalTokens")
        if total_tokens is None and input_tokens is not None and output is not None:
            total_tokens = input_tokens + output
        cost = None
        if self.pricing and uncached is not None and output is not None:
            cost = ((uncached + written) * self.pricing["input"]
                    + cached * self.pricing["cached_input"]
                    + output * self.pricing["output"]) / 1_000_000
        self.record_usage_event(UsageEvent(
            call_type="dsh_native", model=row.get("model") or self.model,
            provider=row.get("provider") or self.provider, input_tokens=input_tokens,
            output_tokens=output, total_tokens=total_tokens,
            reasoning_tokens=usage.get("reasoningTokens"),
            cached_input_tokens=cached, cache_creation_input_tokens=written,
            cost_usd=cost,
            pricing_source="explicit_usd_per_million_tokens" if cost is not None else None,
            pricing_error=None if cost is not None else "No verified pricing supplied or missing provider usage",
            raw_usage=usage, metadata={
                "call_id": row["callId"], "purpose": row.get("purpose"),
                "session_id": row.get("sessionId"), "started_at": row.get("startedAt"),
                "finish": row.get("finish"), "error": row.get("error"),
                "pricing": self.pricing,
            },
        ))

    def observe(self, observation: Observation, next_query: Query | None = None) -> None:
        self._feedback.append(observation.content)
        self._record("queries", "observation", **self._current_query, observation=asdict(observation),
                     next_instance_id=next_query.instance_id if next_query else None,
                     next_instance_index=next_query.instance_index if next_query else None)
        if observation_marks_instance_complete(observation):
            self._record("boundaries", "instance_end", **self._current_query)

    def get_run_artifacts(self) -> dict[str, Any]:
        return {"runtime_directories": list(self._directories), "dsh_bin": self.dsh_bin,
                "profile": "sdk", "model": self.model, "provider": self.provider,
                "reasoning_effort": self.reasoning_effort, "max_tokens": self.max_tokens,
                "max_steps": self.max_steps, "pricing": self.pricing,
                "tools_mode": self.tools_mode}

    def close(self) -> None:
        self._finish_runtime("close")

    def _finish_runtime(self, reason: str) -> None:
        if self._unallocated or self._closed:
            return
        start = time.monotonic()
        self._closed = True
        status, exit_code = self._stop_process(reason)
        self._record("boundaries", "close", reason=reason, status=status, exit_code=exit_code, used=self._used)
        self._write_health(status, exit_code, time.monotonic() - start)

    def _stop_process(self, reason: str) -> tuple[str, int | None]:
        process = self._process
        if process is None:
            return ("process_stopped" if self._used else "unused"), None
        status = "completed"
        try:
            if process.poll() is not None:
                status = "already_exited"
            else:
                self._request("shutdown", {}, time.monotonic() + 10)
                process.wait(timeout=5)
        except (OSError, RuntimeError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
            self._capture_error("shutdown", exc)
            status = "terminated"
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                status = "killed"
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired) as kill_error:
                    self._capture_error("kill", kill_error)
            except ProcessLookupError:
                process.poll()
            except OSError as term_error:
                self._capture_error("terminate", term_error)
        finally:
            try:
                if process.returncode is not None:
                    remaining = time.monotonic() + 1
                    while time.monotonic() < remaining and self._selector.select(0):
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        self._save_stdout(chunk)
            except (OSError, ValueError) as exc:
                self._capture_error("shutdown_stdout", exc)
            self._collect_usage()
            for stream in (process.stdin, process.stdout, self._selector, self._stdout, self._stderr):
                try:
                    stream.close()
                except OSError as exc:
                    self._capture_error("close_handle", exc)
            self._process = None
        self._record("boundaries", "process_exit", reason=reason, status=status, exit_code=process.returncode)
        return status, process.returncode

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
