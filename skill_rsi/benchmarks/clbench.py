"""Drive the pinned official task interface in its own Python environment."""
from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
from pathlib import Path
from threading import Thread

from ..evidence.io import identifier, digest, inside


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
        self.source_manifest = (base / config['source_manifest']).resolve(strict=True) if config.get('source_manifest') else None
        self.source_manifest_section = config.get('source_manifest_section')
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
        if self.source_manifest:
            raw = self.source_manifest.read_bytes()
            manifest = json.loads(raw)
            if self.source_manifest_section:
                manifest = manifest[self.source_manifest_section]
            if manifest.get('commit') != self.revision or not isinstance(manifest.get('files'), dict) or not manifest['files']:
                raise ValueError('official source manifest does not match the pinned revision')
            for name, expected in manifest['files'].items():
                # Source inventories also name public .env.example templates.
                # Evidence redaction paths are a separate policy from checking
                # the bytes of an explicitly declared source snapshot.
                relative = Path(name)
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('source inventory path escapes repository')
                candidate = self.repository
                for component in relative.parts:
                    candidate /= component
                    if candidate.is_symlink():
                        raise ValueError('source inventory path traverses a symlink')
                if digest(candidate.read_bytes()) != expected:
                    raise ValueError('official source manifest hash mismatch: ' + name)
            for path in self.repository.rglob('*.py'):
                if '.git' not in path.parts and path.relative_to(self.repository).as_posix() not in manifest['files']:
                    raise ValueError('undeclared Python source in official snapshot')
            self.source_manifest_sha256 = digest(raw)
            return
        if Path(self._git('rev-parse', '--show-toplevel')).resolve() != self.repository:
            raise ValueError('official source needs its own Git root or an explicit source manifest')
        if self._git("rev-parse", "HEAD") != self.revision:
            raise ValueError("CL-Bench checkout differs from the pinned full commit")
        if self._git("status", "--porcelain", "--untracked-files=no"):
            raise ValueError("CL-Bench official source has tracked modifications")

    def configuration(self):
        return {"kind": "clbench", "revision": self.revision, "tasks": self.specs,
                'source_manifest_sha256': getattr(self, 'source_manifest_sha256', None)}

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
        bridge = Path(__file__).with_name(getattr(benchmark, "worker_file", "clbench_worker.py"))
        self.scorer = getattr(benchmark, "scorer_name", "clbench")
        with (run_dir / "worker.stderr").open("wb") as errors:
            self._process = subprocess.Popen(
                [benchmark.python, "-u", "-c",
                 "import runpy, sys; p=sys.argv.pop(1); runpy.run_path(p, run_name='__main__')", str(bridge), str(run_dir.resolve())],
                cwd=benchmark.repository, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                encoding="utf-8", bufsize=1,
                start_new_session=True,
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
        return {"scorer_version": self.scorer + "@" + self.revision, **self.request("score")}

    def close(self):
        process = self._process
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            from ..adapters.base import stop_process_group
            stop_process_group(process)
        self._reader.join(timeout=1)
        process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
