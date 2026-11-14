"""Discover standard datasets and retain immutable, content-addressed input snapshots."""
import json
from pathlib import Path
import shutil
import tempfile

from .contracts import digest, identifier, load_task_set, make_task_set, strict_json, validate_task_set
from .package_source import PackageTaskPlugin, package_files


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    temporary.replace(path)


class TaskStore:
    def __init__(self, project_root):
        self.project_root = Path(project_root).resolve()
        self.datasets = self.project_root / "integrations/benchmarks"
        self.root = self.project_root / ".runtime/tasksets"

    def discover(self):
        return sorted(path.parent.name for path in self.datasets.glob("*/manifest.json") if path.is_file())

    def register(self, name):
        identifier(name)
        source = self.datasets / name
        if source.is_symlink() or not source.resolve().is_relative_to(self.datasets.resolve()):
            raise ValueError("Dataset escapes benchmarks directory")
        tasks = make_task_set(PackageTaskPlugin(source).load())
        if any(task["benchmark"] != name for task in tasks["tasks"]):
            raise ValueError("Manifest benchmark must equal its directory name")
        from integrations.environments.spec import validate_runtime
        for task in tasks["tasks"]:
            validate_runtime(task, source)
        package_sha = tasks["tasks"][0]["source"]["package_sha256"]
        destination = self.root / "packages" / package_sha
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            stage = Path(tempfile.mkdtemp(prefix=".import-", dir=destination.parent))
            try:
                shutil.copytree(source, stage / "package", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                if digest(package_files(stage / "package")) != package_sha:
                    raise ValueError("Dataset changed while importing")
                try:
                    (stage / "package").rename(destination)
                except FileExistsError:
                    pass
            finally:
                shutil.rmtree(stage)
        if digest(package_files(destination)) != package_sha:
            raise ValueError("Existing package snapshot was modified")
        self.put(tasks)
        registration = {"benchmark": name, "tasks": len(tasks["tasks"]),
            "tasks_sha256": tasks["tasks_sha256"], "package_sha256": package_sha,
            "task_set": str(self.root / "sets" / tasks["tasks_sha256"] / "tasks.json"),
            "package": str(destination)}
        save(self.root / "registrations" / (name + ".json"), registration)
        return registration

    def put(self, task_set):
        tasks = validate_task_set(task_set)
        path = self.root / "sets" / tasks["tasks_sha256"] / "tasks.json"
        if path.exists():
            if load_task_set(path) != tasks:
                raise ValueError("Stored task set was modified")
        else:
            save(path, tasks)
        return path

    def package_for(self, task, *, cache=None):
        package_sha = task["source"].get("package_sha256", "")
        if not isinstance(package_sha, str) or len(package_sha) != 64 or any(c not in "0123456789abcdef" for c in package_sha):
            raise ValueError("Task has no registered package identity; register it first")
        package = self.root / "packages" / package_sha
        # Cache only within one preflight; subsequent runs recheck the snapshot.
        cache = {} if cache is None else cache
        if package_sha not in cache:
            if digest(package_files(package)) != package_sha:
                raise ValueError("Registered package snapshot is unavailable or modified")
            cache[package_sha] = {row["uid"]: row for row in PackageTaskPlugin(package).load()}
        original = cache[package_sha]
        if original.get(task["uid"]) != task:
            raise ValueError("Task differs from its registered package")
        return package
