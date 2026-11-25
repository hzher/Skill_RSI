"""Drive the pinned official task interface in its own Python environment."""
from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
from pathlib import Path
from threading import Thread

from ..evidence.io import identifier


class CLBench:
    def __init__(self, config, base):
        self.repository = (base / config["repository"]).resolve(strict=True)
        python = config.get("python", sys.executable)
        if "/" in python or "\\" in python:
            python = str(base / python)
        self.python = shutil.which(python)
        if not self.python:
            raise ValueError("CL-Bench Python interpreter does not exist")
        self.revision = config["revision"]
        self.timeout = config.get("timeout", 120)
        if type(self.timeout) not in (int, float) or not 0 < self.timeout <= 3600:
            raise ValueError("CL-Bench RPC timeout must be in (0, 3600] seconds")
        self.specs = {}
        for row in config["tasks"]:
            task_id = identifier(row["id"])
            if task_id in self.specs:
                raise ValueError(f"duplicate CL-Bench task {task_id}")
            self.specs[task_id] = {"family": identifier(row["family"]), "parameters": row.get("parameters", {}),
                                   "instance_indices": row.get("instance_indices")}
        if not self.specs:
            raise ValueError("CL-Bench requires at least one task specification")
        self._verify_repository()

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repository, check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20).stdout.strip()

    def _verify_repository(self):
        if self._git("rev-parse", "HEAD") != self.revision:
            raise ValueError("CL-Bench checkout differs from the pinned full commit")
        if self._git("status", "--porcelain", "--untracked-files=no"):
            raise ValueError("CL-Bench official source has tracked modifications")

    def configuration(self):
        return {"kind": "clbench", "revision": self.revision, "tasks": self.specs}

    def tasks(self, split=None):
        return tuple(self.specs)

    def reset(self, task_id, seed, run_dir):
        self._verify_repository()
        runner = OfficialSession(self, run_dir)
        try:
            initial = runner.request("reset", **self.specs[task_id], seed=seed)
        except BaseException:
            runner.close()
            raise
        return runner, initial


class OfficialSession:
    def __init__(self, benchmark, run_dir):
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.revision = benchmark.revision
        self.timeout = benchmark.timeout
        self._replies = queue.Queue()
        bridge = Path(__file__).with_name("clbench_worker.py")
        with (run_dir / "worker.stderr").open("wb") as errors:
            self._process = subprocess.Popen(
                [benchmark.python, "-u", "-c",
                 "import runpy, sys; runpy.run_path(sys.argv[1], run_name='__main__')", str(bridge)],
                cwd=benchmark.repository, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                encoding="utf-8", bufsize=1,
            )
        self._reader = Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        for line in self._process.stdout:
            self._replies.put(line)
        self._replies.put(None)

    def request(self, method, **params):
        try:
            self._process.stdin.write(json.dumps({"method": method, "params": params}, allow_nan=False) + "\n")
            self._process.stdin.flush()
            line = self._replies.get(timeout=self.timeout)
        except (OSError, queue.Empty) as exc:
            raise RuntimeError("official benchmark worker did not respond") from exc
        if line is None:
            raise RuntimeError("official benchmark worker exited")
        result = json.loads(line)
        if "error" in result:
            raise RuntimeError(f"official benchmark {method} failed: {result['error']}")
        return result["result"]

    def step(self, action):
        return self.request("step", action=action)

    def score(self):
        return {"scorer_version": "clbench@" + self.revision, **self.request("score")}

    def close(self):
        process = self._process
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        self._reader.join(timeout=1)
        process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
