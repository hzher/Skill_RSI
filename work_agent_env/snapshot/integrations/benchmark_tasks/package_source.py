"""Public task packages; evaluator code is never imported on load."""
import hashlib
from pathlib import Path

from .contracts import digest, identifier, make_task, strict_json


def package_files(root):
    root = Path(root).resolve()
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Task packages cannot contain symlinks")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


class PackageTaskPlugin:
    """manifest.json plus tasks/<id>/{task_config.json,docs/task.md,...}."""
    def __init__(self, source):
        self.root = Path(source).resolve()

    def read(self, relative, *, optional=False):
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Task package path escapes root")
        if optional and not path.exists():
            return None, None
        raw = path.read_bytes()
        return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()

    def load(self):
        package_sha = digest(package_files(self.root))
        text, manifest_sha = self.read("manifest.json")
        manifest = strict_json(text)
        required = {"schema_version", "benchmark", "revision", "environment", "evaluation", "tasks"}
        if not isinstance(manifest, dict) or set(manifest) != required or manifest["schema_version"] != "workagent-task-package-v1":
            raise ValueError("Invalid task package manifest")
        names = manifest["tasks"]
        if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names):
            raise ValueError("Invalid task package selection")
        if len(names) != len(set(names)):
            raise ValueError("Duplicate task package ID")
        rows = []
        for name in names:
            identifier(name)
            base = "tasks/" + name + "/"
            prompt, prompt_sha = self.read(base + "docs/task.md")
            system, system_sha = self.read(base + "docs/agent_system_prompt.md", optional=True)
            text, config_sha = self.read(base + "task_config.json")
            config = strict_json(text)
            runtime_sha = {}
            for relative in ("preprocess/main.py", "evaluation/main.py"):
                _, sha = self.read(base + relative, optional=True)
                if sha is not None:
                    runtime_sha[relative] = sha
            allowed = {"needed_mcp_servers", "needed_local_tools", "meta", "environment", "evaluation"}
            if not isinstance(config, dict) or set(config) - allowed:
                raise ValueError("Invalid task package config")
            tools = []
            for key, kind in (("needed_mcp_servers", "mcp_server"), ("needed_local_tools", "local_tool")):
                values = config.get(key, [])
                if not isinstance(values, list):
                    raise ValueError("Tool requirements must be lists")
                tools.extend({"kind": kind, "name": value} for value in values)
            rows.append(make_task(benchmark=manifest["benchmark"], task_id=name,
                source={"plugin": "package", "revision": manifest["revision"],
                    "package_sha256": package_sha,
                    "manifest_sha256": manifest_sha, "runtime_input_sha256": runtime_sha, "public_input_sha256": {
                        "prompt": prompt_sha, "system_prompt": system_sha, "config": config_sha}},
                prompt=prompt, system_prompt=system, tools=tools,
                environment=config.get("environment", manifest["environment"]),
                evaluation=config.get("evaluation", manifest["evaluation"]),
                metadata={"task_config": config, "training_admitted": False}))
        return rows
