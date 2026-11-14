"""Adapters run original evaluators; no replacement task-success heuristics."""
import asyncio
import hashlib
import json
from pathlib import Path


class CommandJSONVerifier:
    """Deployment-owned argv/cwd. JSON stdin -> original-evaluator adapter -> JSON stdout.

    A task may select a registered name but cannot supply an executable or cwd.
    Each deployment runs this inside the task's owned environment.
    """
    def __init__(self, *, argv, cwd, files, timeout=180):
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
            raise ValueError("Invalid evaluator argv")
        if not isinstance(files, dict) or not files or timeout <= 0:
            raise ValueError("Evaluator source hashes and timeout are required")
        self.argv, self.cwd, self.files, self.timeout = argv[:], Path(cwd).resolve(), files.copy(), timeout
        self.check_sources()

    def check_sources(self):
        for name, expected in self.files.items():
            path = (self.cwd / name).resolve()
            if not path.is_relative_to(self.cwd) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("Evaluator source identity mismatch")

    async def __call__(self, task, context):
        self.check_sources()
        process = await asyncio.create_subprocess_exec(*self.argv, cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(json.dumps({
                "task": task, "context": context}, allow_nan=False).encode()), self.timeout)
            if process.returncode:
                raise RuntimeError("Evaluator adapter exited unsuccessfully")
            if len(stdout) > 4 * 1024 * 1024:
                raise ValueError("Evaluator output exceeds score budget")
            # Shared JSON boundary rejects duplicate keys and nonfinite values.
            from integrations.benchmark_tasks import strict_json
            return strict_json(stdout.decode("utf-8"))
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
