"""Native Claude Code task tools with one authoritative environment and journal."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import subprocess
import threading
import time

from .storage import atomic, begin, identifier, load, seal


class ActionValidationError(ValueError):
    """Arguments rejected before the benchmark applies any action."""


def tool_result(value, *, error=False):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": error}


def native_task_run_args(run_args, network):
    """Keep offline task containers on a private control network until archived."""
    if "--network=none" not in run_args:
        raise ValueError("Expected the benchmark's declared offline container configuration")
    return ["--network=" + network if value == "--network=none" else value
            for value in run_args if value != "--rm"]


class TaskBroker:
    """Persist tool intent before applying an official task action exactly once."""
    def __init__(self, task, path, *, owner=None):
        self.task = task
        self.path = Path(path)
        self.lock = threading.Lock()
        self.failed = None
        self.owner = owner

    def invoke(self, method, *args):
        function = getattr(self.task, method)
        if self.owner:
            return self.owner.submit(function, *args).result()
        return function(*args)

    def call(self, operation_id, tool, arguments):
        identifier(operation_id)
        with self.lock:
            path = self.path / operation_id
            request = {"tool": tool, "arguments": arguments}
            if (path / "intent.json").exists() and not (path / "result.json").exists():
                raise RuntimeError("Pending task action requires reconciliation")
            cached = begin(path, request)
            if cached is not None:
                return cached
            if self.failed:
                return seal(path, tool_result({"status": "interrupted", "error": self.failed}, error=True))
            try:
                if not isinstance(arguments, dict):
                    raise ActionValidationError("Tool arguments must be an object")
                if tool == "status":
                    if arguments:
                        raise ActionValidationError("status takes no arguments")
                    value = self.invoke("status")
                elif tool == "act":
                    if set(arguments) != {"action"} or not isinstance(arguments["action"], dict):
                        raise ActionValidationError("act requires one action object")
                    value = self.invoke("act", arguments["action"])
                else:
                    raise ActionValidationError("Unknown benchmark tool")
                result = tool_result(value)
            except ActionValidationError as error:
                result = tool_result({"validation_error": str(error), "current": self.invoke("status")}, error=True)
            except Exception as error:
                self.failed = type(error).__name__ + ": " + str(error)
                result = tool_result({"status": "interrupted", "error": self.failed}, error=True)
            return seal(path, result)


class TaskServer:
    def __init__(self, broker, bind):
        self.token = secrets.token_urlsafe(32)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/" + server.token:
                    self.send_error(404)
                    return
                try:
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    result = broker.call(request["operation_id"], request["tool"], request.get("arguments", {}))
                    body = json.dumps(result, ensure_ascii=False).encode()
                    self.send_response(200)
                except Exception as error:
                    body = json.dumps({"error": type(error).__name__, "message": str(error)}).encode()
                    self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer((bind, 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def native_arguments(container, cwd, runtime, api_base, model, max_turns):
    """Full default prompt, isolated settings, and native MCP task tools."""
    return [
        "docker", "exec", "-i", "-w", cwd,
        "-e", "HOME=" + runtime + "/home",
        "-e", "CLAUDE_CONFIG_DIR=" + runtime + "/home/.claude",
        "-e", "CLAUDE_CODE_SIMPLE=",
        "-e", "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
        "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
        "-e", "ANTHROPIC_BASE_URL=" + api_base,
        "-e", "ANTHROPIC_API_KEY=local-recording-bridge",
        container, runtime + "/claude", "--print", "--verbose",
        "--output-format", "stream-json", "--setting-sources", "",
        "--strict-mcp-config", "--mcp-config", runtime + "/mcp.json",
        "--settings", runtime + "/settings.json",
        "--disable-slash-commands", "--tools", "",
        "--allowedTools", "mcp__benchmark__status,mcp__benchmark__act",
        "--model", model, "--effort", "high", "--max-turns", str(max_turns),
    ]


def summarize_native(events):
    calls, returns = {}, {}
    inits, terminals = [], []
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            inits.append(event)
        if event.get("type") == "result":
            terminals.append(event)
        content = event.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                calls[block["id"]] = block
            elif block.get("type") == "tool_result":
                returns[block["tool_use_id"]] = block
    missing = sorted(set(calls) - set(returns))
    terminal = terminals[-1] if terminals else None
    return {
        "complete": bool(terminal and not terminal.get("is_error") and not missing),
        "initializations": inits, "terminal": terminal,
        "tool_calls": list(calls.values()), "tool_returns": list(returns.values()),
        "missing_tool_returns": missing,
    }


def run_native(argv, prompt, path, timeout, interrupt):
    """Write native output continuously, including before a timeout or exit failure."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "prompt.txt").write_text(prompt)
    started = time.time()
    with (path / "native-cli.jsonl").open("wb") as output, (path / "native-stderr.txt").open("wb") as errors:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output, stderr=errors)
        atomic(path / "launch.json", {"pid": process.pid, "started": started})
        timed_out = False
        try:
            process.communicate(prompt.encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            interrupt()
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
    events = []
    for line in (path / "native-cli.jsonl").read_bytes().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    summary = summarize_native(events)
    summary.update(exit_code=process.returncode, timeout=timed_out, seconds=time.time() - started)
    summary["complete"] = summary["complete"] and process.returncode == 0 and not timed_out
    atomic(path / "native-exit.json", {"exit_code": process.returncode, "timeout": timed_out, "seconds": summary["seconds"]})
    atomic(path / "native-summary.json", summary)
    return summary
