"""Durable task records and detached host launches without a background daemon."""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import hashlib
import os
import fcntl
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

from ..components._local import state_root
from ..evidence.io import file_lock, read_json, source_bytes, write_json

_ACTIVE = {"queued", "running", "cancelling"}
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,159}\Z")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _folder_word(value, limit):
    cleaned = re.sub(r"[^\w.-]+", "-", str(value), flags=re.UNICODE).strip(".-") or "task"
    return cleaned.encode("utf-8")[:limit].decode("utf-8", errors="ignore").rstrip(".-") or "task"


def tasks_root(directory=None, *, state_dir=None):
    configured = os.environ.get("SKILL_RSI_TASKS_DIR")
    if directory is None and not configured:
        settings = state_root(state_dir) / "tasks-settings.json"
        configured = read_json(settings).get("root") if settings.is_file() else None
    return Path(directory or configured or Path.home() / "SkillRSI" / "tasks").expanduser().resolve()


def _process_stamp(pid):
    if type(pid) is not int or pid <= 1:
        return None
    try:
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
                                capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    fields = result.stdout.strip().split()
    if result.returncode or len(fields) < 6 or fields[-1].startswith("Z"):
        return None
    return " ".join(fields[:-1])


def _alive(record):
    path = Path(record["path"]) / ".worker.lock"
    with path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def _group_running(group):
    try:
        result = subprocess.run(["/bin/ps", "-A", "-o", "pgid=", "-o", "stat="],
                                capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return None
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == str(group) and not fields[1].startswith("Z"):
            return True
    return False


def _wait_group(group, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        running = _group_running(group)
        if running is not True:
            return running
        time.sleep(0.05)
    return True


def _cleanup_child(record):
    group = record.get("child_pid")
    if not group or _group_running(group) is False:
        return None
    stamp = record.get("child_stamp")
    if not stamp or _process_stamp(group) != stamp:
        return "Cannot verify the native child process identity; termination remains unknown"
    try:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                return None
            running = _wait_group(group, 2)
            if running is False:
                return None
            if running is None:
                return "Cannot confirm native process group termination after the stop request"
    except PermissionError:
        return "Permission denied while stopping the native process group"
    return "The native process group is still running after TERM and KILL"


def _lost_worker(record):
    error = _cleanup_child(record)
    record.update(status="unknown" if error else "interrupted", cleanup_pending=bool(error),
                  cleanup_error=error, error="Task worker stopped unexpectedly")
    if error:
        record.pop("finished_at", None)
    else:
        record["finished_at"] = _now()


@contextmanager
def _edit(path):
    with file_lock(path / ".task.lock"):
        record = read_json(path / "task.json")
        yield record
        record["updated_at"] = _now()
        write_json(path / "task.json", record)


def track_child(options, pid):
    task = options.get("task_dir") or os.environ.get("SKILL_RSI_TASK_DIR")
    if not task:
        return
    path = Path(task)
    with _edit(path) as record:
        record.update(child_pid=pid, child_stamp=_process_stamp(pid))


class TaskStore:
    def __init__(self, tasks_dir=None, *, state_dir=None):
        self.state_dir = state_root(state_dir)
        self.root = tasks_root(tasks_dir, state_dir=self.state_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if tasks_dir is not None:
            write_json(self.state_dir / "tasks-settings.json", {"root": str(self.root)})

    @contextmanager
    def _catalog(self):
        filename = self.root / ".tasks.sqlite3"
        descriptor = os.open(filename, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(filename, timeout=30)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY,path TEXT NOT NULL)")
            with connection:
                yield connection
        finally:
            connection.close()

    def _remember(self, task_id, path):
        with self._catalog() as connection:
            connection.execute("INSERT INTO tasks VALUES (?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path WHERE tasks.path<>excluded.path",
                               (task_id, str(path.relative_to(self.root))))

    def _inside(self, path):
        relative = path.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            if part in {".", ".."}:
                raise ValueError("Task path leaves the configured root")
            current /= part
            if current.is_symlink():
                raise ValueError("Task path traverses a symbolic link")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Task path leaves the configured root")
        return path

    def _folders(self):
        return (*self.root.glob("*/task.json"), *self.root.glob("*/*/*/task.json"))

    def path(self, task_id):
        if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
            raise ValueError("Invalid task identifier")
        with self._catalog() as connection:
            row = connection.execute("SELECT path FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row:
            path = self._inside(self.root / row[0])
            if self._inside(path / "task.json").is_file():
                return path
        for header in self._folders():
            try:
                self._inside(header)
                if read_json(header).get("task_id") == task_id:
                    self._remember(task_id, header.parent)
                    return header.parent
            except (OSError, ValueError):
                continue
        raise ValueError(f"Unknown task: {task_id}")

    def create(self, host, project, prompt=None, *, title=None, task_id=None, mode="managed", source=None, created_at=None):
        from ..adapters.registry import available

        if host not in available():
            raise ValueError(f"Unsupported host: {host}")
        project = Path(project).expanduser().resolve(strict=True)
        if not project.is_dir():
            raise ValueError("Task project must be a directory")
        if prompt is not None and not isinstance(prompt, str):
            raise ValueError("Task prompt must be text")
        if title is not None and not isinstance(title, str):
            raise ValueError("Task title must be text")
        task_id = task_id or datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:12]
        if not _ID.fullmatch(task_id):
            raise ValueError("Invalid task identifier")
        now = _now()
        source = source or ("managed" if mode == "managed" else "analysis" if mode == "llm_analysis" else "observed")
        if not isinstance(source, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", source):
            raise ValueError("Invalid task source")
        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00")) if isinstance(created_at, str) else datetime.now().astimezone()
        date = stamp.astimezone().strftime("%Y-%m-%d")
        topic = (title or prompt or f"{host} session").strip()[:160]
        suffix = hashlib.sha256(task_id.encode()).hexdigest()[:12]
        folder = f"{date}_{_folder_word(topic,72)}_{_folder_word(project.name,48)}_{suffix}"
        path = self.root / host / source / folder
        self._inside(path)
        record = {"schema": "skill-rsi.task.v1", "task_id": task_id,
                  "title": topic, "source": source,
                  "host": host, "project": str(project), "prompt": prompt,
                  "mode": mode, "status": "queued" if mode in {"managed", "llm_analysis"} else "observing",
                  "created_at": created_at or now, "updated_at": now, "path": str(path),
                  "capture_path": str(path / "capture"), "journal_path": str(path / "journal")}
        with file_lock(self.root / ".task-create.lock"):
            try:
                self.path(task_id)
            except ValueError:
                pass
            else:
                raise ValueError(f"Task already exists: {task_id}")
            path.mkdir(mode=0o700, parents=True, exist_ok=False)
            write_json(path / "task.json", record)
            self._remember(task_id, path)
        return record

    def start(self, host, project, prompt, *, title=None, resume=None, options=None, mode="managed"):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Enter a task prompt")
        if mode not in {"managed", "llm_analysis"}:
            raise ValueError("Only managed or analysis tasks can be launched")
        options = dict(options or {})
        if "output" in options:
            raise ValueError("UI tasks always use their own capture directory")
        record = self.create(host, project, prompt, title=title, mode=mode)
        path = self.path(record["task_id"])
        write_json(path / "request.json", {"resume": resume, "options": options})
        argv = [sys.executable, "-m", "skill_rsi.tasks.worker", "--task", str(path),
                "--state-dir", str(self.state_dir)]
        env = {**os.environ, "SKILL_RSI_TASK_DIR": str(path), "SKILL_RSI_TASKS_DIR": str(self.root)}
        try:
            with (path / "worker.log").open("xb") as log:
                os.fchmod(log.fileno(), 0o600)
                process = subprocess.Popen(argv, cwd=record["project"], env=env, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log, start_new_session=True)
            with _edit(path) as current:
                current.update(worker_pid=process.pid, worker_stamp=_process_stamp(process.pid))
        except OSError as error:
            with _edit(path) as current:
                current.update(status="failed", finished_at=_now(), error=str(error))
            raise
        return self.get(record["task_id"])

    def get(self, task_id):
        path = self.path(task_id)
        record = read_json(path / "task.json")
        exited = False
        pid = record.get("worker_pid")
        if type(pid) is int:
            try:
                exited = os.waitpid(pid, os.WNOHANG)[0] == pid
            except ChildProcessError:
                pass
        started = record.get("started_at")
        stale = (datetime.now(timezone.utc) - datetime.fromisoformat(record["created_at"])).total_seconds() > 30
        unfinished = record["status"] in _ACTIVE or record.get("cleanup_pending")
        if unfinished and (started or exited or stale) and not _alive(record):
            with _edit(path) as current:
                if (current["status"] in _ACTIVE or current.get("cleanup_pending")) and not _alive(current):
                    _lost_worker(current)
            record = read_json(path / "task.json")
        return record

    def list(self):
        records = []
        for header in self._folders():
            path = header.parent
            try:
                self._inside(header)
                task_id = read_json(header)["task_id"]
                self._remember(task_id, path)
                records.append(self.get(task_id))
            except (OSError, ValueError, KeyError):
                continue
        return sorted(records, key=lambda item: item["created_at"], reverse=True)

    def detail(self, task_id):
        record = self.get(task_id)
        path = self.path(task_id)
        capture = Path(record["capture_path"])
        launch = read_json(capture / "launch.json") if (capture / "launch.json").is_file() else None
        files = [str(file.relative_to(path)) for file in path.rglob("*") if file.is_file() and not file.is_symlink()][:300]
        events, decisions = _events(path / "journal")
        native_tail = _tail(capture / "codex-rollout.jsonl")
        child_streams = sorted((capture / "threads").glob("*/codex-rollout.jsonl"), key=lambda item: item.stat().st_mtime)
        if not native_tail and child_streams:
            native_tail = _tail(child_streams[-1])
        if native_tail:
            for line in native_tail.splitlines():
                try:
                    events.append(json.loads(line))
                except (ValueError, UnicodeError):
                    continue
            events = events[-50:]
        return {**record, "launch": launch, "stdout": _tail(capture / "stdout.jsonl") or native_tail,
                "stderr": _tail(capture / "stderr.log"), "worker_log": _tail(path / "worker.log"),
                "events": events, "decisions": decisions, "files": files}

    def cancel(self, task_id):
        path = self.path(task_id)
        with _edit(path) as record:
            if record["status"] not in _ACTIVE and not record.get("cleanup_pending"):
                return dict(record)
            record.update(status="cancelling", cancel_requested=True)
            write_json(path / "cancel.json", {"requested_at": _now()})
            alive = _alive(record)
            if alive:
                try:
                    os.kill(record["worker_pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif record.get("started_at") or record.get("cleanup_pending"):
                _lost_worker(record)
        return self.get(task_id)


def _tail(path, size=32768):
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - size))
        return stream.read(size).decode("utf-8", errors="replace")


def _events(journal):
    database = journal / "journal.sqlite3"
    if not database.is_file():
        return [], []
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
        rows = connection.execute("SELECT payload FROM events ORDER BY ordinal DESC LIMIT 50").fetchall()
    events = [json.loads(row[0]) for row in reversed(rows)]
    decisions = []
    for event in events:
        if event.get("type") not in {"component_decision", "component_receipt"}:
            continue
        source = json.loads(source_bytes(journal, event["raw_ref"]))
        if isinstance(source.get("decision"), dict):
            decisions.append(source["decision"])
    return events, decisions


def run_task(host, project, state_dir, *, prompt=None, resume=None, options=None, command=None, task=None):
    options = dict(options or {})
    store = TaskStore(options.pop("tasks_dir", None), state_dir=state_dir)
    record = task or store.create(host, project, prompt, title=options.pop("title", None))
    path = Path(record["path"])
    with file_lock(path / ".worker.lock"):
        return _execute_task(record, project, state_dir, prompt, resume, options, command)


def _execute_task(record, project, state_dir, prompt, resume, options, command):
    from ..adapters.registry import get_adapter

    path = Path(record["path"])
    selected_output = options.get("output")
    capture = Path(selected_output).expanduser().resolve() if selected_output is not None else path / "capture"
    options.update(output=str(capture), task_dir=str(path))
    previous = os.environ.get("SKILL_RSI_TASK_DIR")
    os.environ["SKILL_RSI_TASK_DIR"] = str(path)
    with _edit(path) as current:
        current.update(status="running", capture_path=str(capture), started_at=_now(),
                       worker_pid=os.getpid(), worker_stamp=_process_stamp(os.getpid()))
    try:
        if (path / "cancel.json").exists():
            raise KeyboardInterrupt()
        result = get_adapter(record["host"]).launch(command, project=project, state_dir=state_dir,
                                          prompt=prompt, resume=resume, options=options)
    except BaseException as error:
        with _edit(path) as current:
            current.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                           finished_at=_now(), error=str(error) or type(error).__name__)
        raise
    finally:
        if previous is None:
            os.environ.pop("SKILL_RSI_TASK_DIR", None)
        else:
            os.environ["SKILL_RSI_TASK_DIR"] = previous
    with _edit(path) as current:
        current.update(status=result["status"], finished_at=_now(),
                       run_id=result.get("run_id"), session_id=result.get("session_id"),
                       final_response=result.get("final_response"), returncode=result.get("returncode"),
                       missing_reason=result.get("missing_reason"))
        if result["status"] == "interrupted":
            cleanup_error = _cleanup_child(current)
            if cleanup_error:
                current.update(status="unknown", cleanup_pending=True, cleanup_error=cleanup_error)
                current.pop("finished_at", None)
    return {**result, "status": current["status"], "task_id": record["task_id"], "task_path": str(path)}


def managed_session_task(project, host, session, *, state_dir):
    """Associate a native thread with its managed launch, including live stdout."""
    store = TaskStore(state_dir=state_dir)
    pending = False
    for task in store.list():
        if task["mode"] not in {"managed", "llm_analysis"} or task["host"] != host or task["project"] != str(project):
            continue
        if task.get("session_id") == session:
            return task, pending
        announced = task.get("session_id")
        output = Path(task["capture_path"]) / "stdout.jsonl"
        if announced is None and output.is_file():
            with output.open("rb") as stream:
                for _ in range(128):
                    raw = stream.readline(1024 * 1024)
                    if not raw.endswith(b"\n"):
                        break
                    try:
                        row = json.loads(raw)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(row, dict) and row.get("type") == "thread.started":
                        announced = row.get("thread_id")
                        if isinstance(announced, str):
                            with _edit(Path(task["path"])) as current:
                                current["session_id"] = announced
                            task["session_id"] = announced
                        break
        if announced == session:
            return task, pending
        pending |= task["status"] in _ACTIVE and not announced
    return None, pending


def session_task(project, host, session, *, state_dir, live=False, source=None, title=None, created_at=None):
    from ..evidence.schema import identifier

    store = TaskStore(state_dir=state_dir)
    task_id = "session-" + identifier(str(project), host, session)
    with file_lock(store.root / ".sessions.lock"):
        try:
            path = store.path(task_id)
        except ValueError:
            task = store.create(host, project, title=title or f"{host} · {session}", task_id=task_id, mode="observed",
                                source=source, created_at=created_at)
            path = Path(task["path"])
    with _edit(path) as task:
        task["session_id"] = session
        if live:
            task.update(live_callback_pending=False, last_live_callback_at=_now())
    return read_json(path / "task.json")


def record_event(directory, record):
    """Route observed hooks to the managed task or to a stable native session folder."""
    from ..evidence.journal import EventJournal

    registration = read_json(Path(directory) / "registration.json")
    state_dir = Path(directory).parent.parent
    selected = os.environ.get("SKILL_RSI_TASK_DIR")
    if selected:
        path = Path(selected).resolve(strict=True)
        task = read_json(path / "task.json")
        if task["project"] != registration["project"] or task["host"] != registration["host"]:
            raise ValueError("Observed event belongs to a different task project or host")
    else:
        session = record.get("native_session_id") or record["run_id"]
        task = session_task(registration["project"], registration["host"], session, state_dir=state_dir, source="hook")
        path = Path(task["path"])
        with _edit(path) as task:
            task["run_id"] = record["run_id"]
    EventJournal(path / "journal").append(record)
    with _edit(path) as task:
        task.update(live_callback_pending=False, last_live_callback_at=_now())
