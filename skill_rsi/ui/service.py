"""Project-scoped management shared by HTTP actions and the existing runtime."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from pathlib import Path
import os
import shutil
import subprocess
import sys
import threading
from time import monotonic

from ..adapters.base import host_record, read_optional
from ..adapters.registry import available, get_adapter
from ..components import ComponentRegistry, builtin_catalog
from ..components.description import describe
from ..components._local import state_root
from ..evidence.io import file_lock, write_json
from ..pipelines.setup import setup
from ..runtime import Runtime, project_identity

LABELS = {"dsh": "DSH", "codex": "Codex", "claude_code": "Claude Code"}
SCOPE_FIELDS = ("mcp_paths", "checkpoint_paths", "constraints")


def find_command(host):
    adapter = get_adapter(host)
    found = shutil.which(adapter.command)
    if found:
        return found
    roots = [Path(sys.executable).parent, Path.home() / ".local/bin",
             Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    for conda in ("miniconda3", "anaconda3"):
        base = Path.home() / conda
        roots.append(base / "bin")
        envs = base / "envs"
        if envs.is_dir():
            roots.extend(child / "bin" for child in sorted(envs.iterdir()) if child.is_dir())
    if host == "codex":
        roots.append(Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS"))
    return next((str(root / adapter.command) for root in roots
                 if (root / adapter.command).is_file() and os.access(root / adapter.command, os.X_OK)), adapter.command)


class Manager:
    def __init__(self, project, state_dir, tasks_dir=None):
        from ..tasks import TaskStore

        self.project = self._project(project)
        self.state_dir = state_root(state_dir)
        self.registry = ComponentRegistry(self.state_dir)
        self.tasks = TaskStore(tasks_dir, state_dir=self.state_dir)
        self._lock = threading.RLock()
        self._probes = {}
        self._preferences = self.state_dir / "manager.json"
        self._recording_stop = threading.Event()
        self._recording_thread = None
        self._recovery_thread = None

    def start_recording(self):
        """Keep native collection independent of the currently selected UI tab."""
        from ..adapters.codex.recording import watch

        with self._lock:
            if self._recording_thread is not None:
                return
            self._recording_thread = threading.Thread(
                target=watch, args=(self.state_dir,),
                kwargs={"stop_event": self._recording_stop},
                name="codex-recording", daemon=True)
            self._recording_thread.start()
            self._recovery_thread = threading.Thread(
                target=self._recover_generations, name="harness-finalizers", daemon=True)
            self._recovery_thread.start()

    def _recover_generations(self):
        from ..emergence import recover

        recover(self.tasks, state_dir=self.state_dir)

    def close(self):
        self._recording_stop.set()
        if self._recording_thread is not None:
            self._recording_thread.join(timeout=5)

    @staticmethod
    def _project(path):
        project = Path(path).expanduser().resolve(strict=True)
        if not project.is_dir():
            raise ValueError("请选择已经存在的项目文件夹")
        return project

    def _projects(self, tasks=()):
        saved = read_optional(self._preferences) or {}
        paths = {str(self.project), *saved.get("projects", [])}
        paths.update(task["project"] for task in tasks if task.get("project"))
        for file in (self.state_dir / "hosts").glob("*/registration.json"):
            row = read_optional(file) or {}
            if row.get("project"):
                paths.add(row["project"])
        return sorted(paths)

    def _probe(self, project, host, *, refresh=False):
        key = str(project), host
        with self._lock:
            stamp, cached = self._probes.get(key, (0, None))
        if not refresh and cached is not None and monotonic() - stamp < 15:
            return cached
        adapter = get_adapter(host)
        registration = read_optional(host_record(project, self.state_dir, host)) or {}
        command = registration.get("command") or find_command(host)
        probe = adapter.probe(project, self.state_dir, command=command)
        result = {**probe, "id": host, "label": LABELS.get(host, host),
                  "version": probe["host_version"], "command": command}
        with self._lock:
            self._probes[key] = monotonic(), result
        return result

    def state(self, project=None, host="dsh", *, refresh=False):
        project = self._project(project or self.project)
        get_adapter(host)
        runtime = Runtime(project, state_dir=self.state_dir)
        with ThreadPoolExecutor(max_workers=3) as pool:
            hosts = list(pool.map(lambda name: self._probe(project, name, refresh=refresh), available()))
        record = read_optional(host_record(project, self.state_dir, host)) or {}
        if not record:
            local = read_optional(project / "skill-rsi.json") or {}
            if local.get("host") == host:
                record = local
        config = {key: record[key] for key in ("command", "profile", "observe", *SCOPE_FIELDS) if key in record}
        config.setdefault("observe", True)
        desktop = read_optional(host_record(project, self.state_dir, host).parent / "desktop.json") or {}
        config["desktop_enabled"] = desktop.get("phase") == "registered"
        if config["desktop_enabled"]:
            config["connection_mode"] = "desktop"
        preferences = read_optional(self._preferences) or {}
        selected_mode = preferences.get("connection_modes", {}).get(f"{runtime.project_id}/{host}")
        if selected_mode in {"cli", "desktop"}:
            config["connection_mode"] = selected_mode
        if host == "dsh" and config.get("connection_mode") == "desktop":
            from ..adapters.dsh.desktop import status_desktop

            desktop_status = status_desktop(project, self.state_dir)
            config["desktop_status"] = desktop_status
            for description in hosts:
                if description["id"] == host:
                    description["loaded"] = desktop_status["loaded"]
                    description["configured"] = desktop_status["configured"]
        if host == "codex":
            from ..adapters.codex.recording import status

            config["codex_recording"] = status(self.state_dir, project=project)
        bindings = {binding.component_id: binding for binding in runtime.bindings(host)}
        releases = self.registry.list()
        extensions = []
        known = set()
        for release in releases:
            if host not in release.manifest["scope"]["hosts"] and "*" not in release.manifest["scope"]["hosts"]:
                continue
            known.add(release.component_id)
            binding = bindings.get(release.component_id)
            selected = binding and (binding.version, binding.form) == (release.version, release.form)
            extensions.append({
                **release.to_dict(), "id": release.component_id, "source": str(release.path),
                "enabled": bool(selected and binding.enabled),
                "mode": binding.mode if selected else "shadow",
                "options": binding.options if selected else {},
                "modes": release.manifest["scope"]["modes"],
                "status": binding.status if selected else "available",
                "error": binding.activation_error if selected else None,
            })
            extensions[-1].update(describe(release.component_id, release.path))
        catalog = builtin_catalog()
        for item in catalog:
            if item["component_id"] in known:
                continue
            if host not in item["scope"]["hosts"] and "*" not in item["scope"]["hosts"]:
                continue
            extensions.append({**item, "id": item["component_id"], "source": "builtin:" + item["component_id"],
                               "enabled": False, "mode": "shadow", "modes": item["scope"]["modes"],
                               "options": {}, "status": "available"})
            resource = files("skill_rsi.components.resources").joinpath(item["component_id"])
            extensions[-1].update(describe(item["component_id"], resource))
            extensions[-1].pop("storage_path", None)
        tasks = [task for task in self.tasks.list() if task.get("mode") != "llm_analysis"]
        return {"project": str(project), "host": host, "hosts": hosts, "config": config,
                "extensions": extensions, "catalog": catalog, "projects": self._projects(tasks),
                "tasks_dir": str(self.tasks.root),
                "tasks": tasks}

    def configure(self, data):
        project, host = self._project(data["project"]), data["host"]
        adapter = get_adapter(host)
        options = {key: data[key] for key in ("command", "profile", "observe", *SCOPE_FIELDS) if key in data}
        if not options.get("command"):
            options["command"] = find_command(host)
        options.setdefault("observe", True)
        for key in ("command", "profile"):
            if key in options and options[key] is not None and not isinstance(options[key], str):
                raise ValueError(f"{key} 必须填写文本")
        with self._lock, file_lock(self.state_dir / ".manager.lock"):
            result = setup(adapter, project, self.state_dir, options=options, extensions=data.get("extensions", []),
                           base=project, reconcile=True, manage_all=True)
            preferences = read_optional(self._preferences) or {}
            preferences["projects"] = sorted({*preferences.get("projects", []), str(project)})
            connection_mode = data.get("connection_mode", "cli")
            if connection_mode in {"cli", "desktop"}:
                key = f"{project_identity(project)}/{host}"
                preferences.setdefault("connection_modes", {})[key] = connection_mode
            write_json(self._preferences, preferences)
            self._probes.pop((str(project), host), None)
        return result

    def codex_recording(self, data):
        from ..adapters.codex.recording import configure

        if type(data.get("enabled")) is not bool:
            raise ValueError("enabled 必须为布尔值")
        project = self._project(data["project"])
        result = configure(project, self.state_dir, enabled=data["enabled"],
                           codex_home=data.get("codex_home"), tasks_dir=self.tasks.root,
                           scope=data.get("scope", "all_projects"))
        with self._lock, file_lock(self.state_dir / ".manager.lock"):
            preferences = read_optional(self._preferences) or {}
            preferences["projects"] = sorted({*preferences.get("projects", []), str(project)})
            write_json(self._preferences, preferences)
        return result

    def codex_sync(self, data):
        from ..adapters.codex.recording import sync

        if data.get("scope", "all_projects") == "all_projects":
            return sync(self.state_dir)
        return sync(self.state_dir, project=self._project(data["project"]))

    def install(self, data):
        source = data.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("请填写扩展 ZIP 或目录的本机路径")
        return self.registry.install(source).to_dict()

    def desktop(self, data):
        from ..adapters.dsh.desktop import install_desktop

        if data.get("host") != "dsh":
            raise ValueError("桌面接入当前用于 DSH")
        self.configure(data)
        project = self._project(data["project"])
        result = install_desktop(project, self.state_dir, observe=data.get("observe", True))
        self._probes.pop((str(project), "dsh"), None)
        return {**result, "message": "已登记桌面插件。请完全退出并重新打开 DSH 桌面端，再在所选项目中继续任务。"}

    def remove_desktop(self, data):
        from ..adapters.dsh.desktop import uninstall_desktop

        result = uninstall_desktop(self._project(data["project"]), self.state_dir)
        self._probes.pop((str(self._project(data["project"])), "dsh"), None)
        return result

    def analyze(self, data):
        if data.get("method", "emergence") == "emergence":
            return self.emerge(data)
        path = self.tasks.path(data["task_id"])
        if data.get("method", "llm") == "local":
            from ..discovery.task_analysis import analyze_task
            result = analyze_task(path)
        else:
            from ..discovery.llm_analysis import start_analysis
            host = data.get("host", "codex")
            result = start_analysis(path, self.tasks, self.state_dir,
                                    host="dsh" if host == "api" else host,
                                    backend="http" if host == "api" else "cli")
        result["method"] = data.get("method", "llm")
        write_json(path / "analysis-result.json", result)
        return result

    def analysis(self, task_id):
        from ..discovery.llm_analysis import poll_analysis

        path = self.tasks.path(task_id)
        selected = read_optional(path / "analysis-result.json")
        if selected and selected.get("method") == "local":
            return selected
        result = poll_analysis(path, self.tasks)
        if result is None:
            return read_optional(path / "analysis-result.json") or {"status": "idle"}
        job = result.get("analysis_job")
        if isinstance(job, dict):
            detail = self.tasks.detail(job["task_id"])
            result["diagnostics"] = "\n\n".join(
                str(value) for value in (job.get("error"), detail.get("stderr"), detail.get("worker_log")) if value)
        result["method"] = "llm"
        write_json(path / "analysis-result.json", result)
        return result

    def llm(self):
        from ..discovery.llm_settings import get_settings
        return get_settings(self.state_dir)

    def save_llm(self, data):
        from ..discovery.llm_settings import save_settings
        return save_settings(self.state_dir, data)

    def emergence_settings(self):
        from ..discovery.codex_settings import get_settings
        return get_settings(self.state_dir)

    def save_emergence_settings(self, data):
        from ..discovery.codex_settings import save_settings
        return save_settings(self.state_dir, data)

    def emerge(self, data):
        from ..emergence import start

        settings = self.emergence_settings()
        options = {key: data.get(key) or settings[key] for key in ("model", "effort", "command")}
        return start(self.tasks.path(data["task_id"]), self.tasks, self.state_dir,
                     instructions=data.get("instructions", ""), **options)

    def emergence(self, task_id):
        from ..emergence import poll

        result = poll(self.tasks.path(task_id), self.tasks) or {"status": "not_started"}
        for candidate in result.get("candidates", []):
            if candidate.get("source_path"):
                document = describe(candidate.get("component_id", ""), candidate["source_path"])
                if document.get("readme"):
                    candidate["readme"] = document["readme"]
        return result

    def open_path(self, data):
        value = data.get("path")
        if not isinstance(value, str) or not value:
            raise ValueError("请提供本机文件或文件夹路径")
        selected = Path(value).expanduser().resolve(strict=True)
        resources = Path(__file__).resolve().parents[1] / "components/resources"
        roots = (self.tasks.root.resolve(), self.state_dir.resolve(), resources.resolve())
        if not any(selected.is_relative_to(root) for root in roots):
            raise ValueError("只能打开已登记的任务或扩展位置")
        if sys.platform == "darwin":
            argv = ["open", "-R", str(selected)]
        elif sys.platform == "win32":
            argv = ["explorer", "/select,", str(selected)]
        else:
            argv = ["xdg-open", str(selected if selected.is_dir() else selected.parent)]
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"path": str(selected), "status": "opened"}

    def start(self, data):
        project, host = self._project(data["project"]), data["host"]
        adapter = get_adapter(host)
        record = read_optional(host_record(project, self.state_dir, host))
        if not record:
            raise ValueError("请先保存并接入这个项目")
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("请填写任务")
        if len(prompt) > 100_000:
            raise ValueError("任务超过 100000 字符")
        options = {key: record[key] for key in ("command", "profile") if record.get(key)}
        if host == "codex" and data.get("trace") is True:
            options["trace"] = True
        return self.tasks.start(adapter.name, project, prompt, options=options)

    def task(self, task_id):
        detail = self.tasks.detail(task_id)
        task = {key: value for key, value in detail.items()
                if key not in {"launch", "stdout", "stderr", "worker_log", "files", "events", "decisions"}}
        analysis = self.analysis(task_id)
        return {**detail, "task": task, "analysis": analysis, "emergence": self.emergence(task_id)}
