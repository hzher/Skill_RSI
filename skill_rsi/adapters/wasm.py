"""Execute a component decision as a bounded WASI command."""

import json
import os
import selectors
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from ..components.manifest import canonical
from .base import stop_process_group

ABI = "skill-rsi.middleware-stdio.v1"
_MAX_MODULE_BYTES = 8 * 1024 * 1024
_MAX_INPUT_BYTES = 8 * 1024 * 1024
_MAX_OUTPUT_BYTES = 256 * 1024
_FUEL = 10_000_000
_MEMORY = 16 * 1024 * 1024


def available():
    return shutil.which("wasmtime") if os.name == "posix" else None


def _stdout(process, elapsed, limit):
    result = bytearray()
    deadline = monotonic() + elapsed
    with selectors.DefaultSelector() as watcher:
        watcher.register(process.stdout, selectors.EVENT_READ)
        while len(result) <= limit:
            remaining = deadline - monotonic()
            if remaining <= 0 or not watcher.select(remaining):
                raise TimeoutError("Middleware decision exceeded its elapsed-time budget")
            data = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(result)))
            if not data:
                code = process.wait(timeout=max(0, deadline - monotonic()))
                if code:
                    raise RuntimeError("Middleware trapped or stopped with an error")
                return bytes(result)
            result.extend(data)
    raise ValueError("Middleware response exceeded its byte budget")


def decide(context):
    executable = available()
    if executable is None:
        raise ValueError("The optional Wasmtime CLI is unavailable")
    module = context.artifact_file(context.release.manifest["entrypoint"])
    if not module.startswith(b"\x00asm") or len(module) > _MAX_MODULE_BYTES:
        raise ValueError("Middleware requires a bounded binary Wasm module")
    invoke = {"abi": ABI, "method": "decide", "event": asdict(context.event),
              "state": context.state, "options": context.options}
    payload = canonical(invoke) + b"\n"
    if len(payload) > _MAX_INPUT_BYTES:
        raise ValueError("Middleware input exceeded its byte budget")
    budget = min(context.budget["max_decision_ms"], 2000)
    if budget <= 0:
        raise ValueError("Middleware requires a positive execution budget")
    output_limit = min(_MAX_OUTPUT_BYTES,
                       context.budget["max_action_bytes"] + context.budget["max_state_bytes"] + 4096)
    with TemporaryDirectory(prefix="skill-rsi-wasm-") as directory:
        root = Path(directory)
        wasm = root / "policy.wasm"
        wasm.write_bytes(module)
        envelope = root / "input"
        envelope.write_bytes(payload)
        # stdio only. No WASI preopened directories, credentials, native imports, or network.
        argv = [executable, "run",
                "-C", "cache=n,parallel-compilation=n",
                "-W", (f"fuel={_FUEL},max-memory-size={_MEMORY},max-memories=1,max-instances=8,"
                       f"max-tables=2,max-table-elements=10000,threads=n,timeout={budget}ms"),
                "-S", ("inherit-env=n,inherit-network=n,allow-ip-name-lookup=n,tcp=n,udp=n,http=n,nn=n,"
                       "threads=n,tls=n,config=n,keyvalue=n,listenfd=n"),
                str(wasm), "decide"]
        with envelope.open("rb") as stdin, subprocess.Popen(
            argv, cwd=root, env={}, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as process:
            try:
                reply = _stdout(process, budget / 1000, output_limit)
            finally:
                if process.poll() is None:
                    stop_process_group(process)
    value = json.loads(reply)
    if not isinstance(value, dict):
        raise ValueError("Middleware decide must return a JSON object")
    return value
