"""Small shared contracts; file tasks do not import PostgreSQL or MCP runtimes."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
from typing import Protocol
import uuid
from workenv.atomic_io import replace_atomic

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))


class ResourceError(RuntimeError):
    """An owned environment resource is unavailable or has inconsistent identity."""


def now() -> str:
    return datetime.now(TZ).isoformat()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value: object, *, compact: bool = False) -> None:
    """Atomically write JSON; compact layout is opt-in for large snapshots."""
    if type(compact) is not bool:
        raise ValueError("JSON compact option must be boolean")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        layout = {"separators": (",", ":")} if compact else {"indent": 2}
        temporary.write_text(json.dumps(value, ensure_ascii=False, default=str, **layout) + "\n", encoding="utf-8")
        replace_atomic(temporary,path)
    finally:
        try:temporary.unlink(missing_ok=True)
        except OSError:pass  # Preserve the original write error if cleanup is blocked too.


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_directory(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Invalid run ID")
    no_links(ROOT/"runs",ROOT)
    result = (ROOT / "runs" / run_id).resolve()
    if not result.is_relative_to((ROOT / "runs").resolve()):
        raise ValueError("Run directory escapes owned root")
    return result


def no_links(path: Path, boundary: Path) -> None:
    boundary = boundary.absolute()
    current = path.absolute()
    while True:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Linked paths are not supported")
        if current == boundary:
            break
        if current.parent == current or not current.is_relative_to(boundary):
            raise ValueError("Path escapes boundary")
        current = current.parent


def contained(base: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise ValueError("Expected a relative path")
    if ".." in relative.replace("\\", "/").split("/"):
        raise ValueError("Parent traversal is forbidden")
    path = base / relative
    no_links(path, base)
    resolved = path.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError("Path escapes boundary")
    return resolved


def file_snapshot(base: Path) -> dict[str, str]:
    no_links(base, base)
    result = {}
    for p in sorted(base.rglob("*")):
        no_links(p, base)
        if p.is_file():
            result[p.relative_to(base).as_posix()] = digest(p)
    return result


def partial_snapshot(run: Path, error: Exception) -> dict:
    return {"files":file_snapshot(run/"workspace"),"capture_status":"partial",
            "capture_error":type(error).__name__+": "+str(error)}


def source_hashes() -> dict[str, str]:
    return {p.relative_to(ROOT).as_posix(): digest(p)
            for directory in ("workenv", "adapters", "scripts", "configs")
            for p in sorted((ROOT / directory).rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
            and (not p.name.startswith(".env") or p.name == ".env.example")}


def load_task(task_id: str) -> dict:
    tasks = read_json(ROOT / "tasks" / "registry.json")["tasks"]
    matches = [t for t in tasks if t["task_id"] == task_id]
    if len(matches) != 1:
        raise ValueError("Task must occur exactly once in registry: " + task_id)
    spec = matches[0]
    if not spec.get("runtime_adapter"):
        raise ValueError("Task is registered for inspection only: " + task_id)
    return spec


@contextmanager
def run_lock(run: Path):
    no_links(run, ROOT / "runs")
    path = run / ".operation.lock"
    from workenv.leases import owned_lock
    with owned_lock(path,ROOT,"run_operation"):
        yield


class TaskAdapter(Protocol):
    spec: dict
    modes: tuple[str, ...]

    def guard(self): ...
    def create(self, mode: str, run_id: str | None) -> Path: ...
    def reset(self, run: Path, *, archive: bool = True) -> dict: ...
    def snapshot(self, run: Path) -> dict: ...
    async def perform(self, run: Path, mode: str, trace: list) -> str: ...
    def evaluate(self, run: Path, mode: str) -> dict: ...


def owned_run(run: Path) -> dict:
    if run_directory(run.name) != run.resolve():
        raise ValueError("Unowned run path")
    no_links(run, ROOT / "runs")
    metadata = read_json(run / "run.json")
    if metadata["workspace_owner"] != str(ROOT):
        raise ValueError("Run owner mismatch")
    return metadata
