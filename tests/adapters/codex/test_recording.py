"""Regression coverage for native Codex rollout boundaries and task identity."""

import json
from datetime import datetime
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import sqlite3
import os
import tempfile
import unittest
from unittest.mock import patch

from skill_rsi.adapters.codex.recording import configure, status, sync
from skill_rsi.tasks.store import TaskStore, _edit


class NativeRecordingTests(unittest.TestCase):
    def test_exec_source_overrides_inherited_desktop_originator(self):
        from skill_rsi.adapters.codex.recording import _source

        self.assertEqual(_source({"source": "exec", "originator": "Codex Desktop"}), "cli")
        self.assertEqual(_source({"source": "cli", "originator": "Codex Desktop"}), "cli")
        self.assertEqual(_source({"source": "vscode", "originator": "Codex Desktop"}), "desktop")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "working-project"
        self.project.mkdir()
        self.home = self.root / "codex"
        (self.home / "sessions").mkdir(parents=True)
        (self.home / "archived_sessions").mkdir()
        self.state = self.root / "state"
        self.tasks = self.root / "tasks"
        configure(self.project, self.state, codex_home=self.home, tasks_dir=self.tasks, capture_existing=True)

    def write(self, thread="thread-a", *, metadata=None, rows=None):
        body = {"id": thread, "cwd": str(self.project), "source": "cli", "originator": "codex_cli_rs",
                "timestamp": "2026-11-25T08:00:00Z", **(metadata or {})}
        data = [{"type": "session_meta", "payload": body}, *(rows or [])]
        path = self.home / "sessions" / f"rollout-{thread}.jsonl"
        path.write_bytes(b"".join(self.line(row) for row in data))
        return path, data

    @staticmethod
    def line(row):
        return (json.dumps(row) + "\n").encode()

    def test_complete_line_cursor_archive_move_and_rewritten_tail(self):
        one = {"type": "event_msg", "payload": {"type": "user_message", "message": "one"}}
        two = {"type": "event_msg", "payload": {"type": "agent_message", "message": "two"}}
        source, rows = self.write(rows=[one])
        with source.open("ab") as stream:
            stream.write(self.line(two)[:-1])
        result = sync(self.state)
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["phase"], "collecting")
        with source.open("ab") as stream:
            stream.write(b"\n")
        self.assertEqual(sync(self.state)["records"], 1)
        self.assertEqual(sync(self.state)["records"], 0)
        task, = TaskStore(state_dir=self.state).list()
        captured = Path(task["capture_path"]) / "codex-rollout.jsonl"
        self.assertEqual(captured.read_bytes(), b"".join(self.line(row) for row in [*rows, two]))
        archived = self.home / "archived_sessions" / source.name
        source.rename(archived)
        self.assertEqual(sync(self.state)["records"], 0)
        changed = {"type": "event_msg", "payload": {"type": "agent_message", "message": "new"}}
        archived.write_bytes(b"".join(self.line(row) for row in [*rows, changed]))
        self.assertEqual(sync(self.state)["records"], 1)
        self.assertEqual(len(TaskStore(state_dir=self.state).list()), 1)
        self.assertIn(self.line(two), captured.read_bytes())
        self.assertTrue(captured.read_bytes().endswith(self.line(changed)))

    def test_allowlist_source_title_lineage_and_safe_layout(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as connection:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY,name TEXT)")
            connection.execute("INSERT INTO threads VALUES('thread-a','Repair / unit conversion')")
            connection.commit()
        source, _ = self.write(metadata={"source": "vscode", "thread_source": "user", "parent_thread_id": "parent",
                                          "originator": "Codex Desktop", "forked_from_id": "fork"})
        other = self.root / "unregistered"
        other.mkdir()
        self.write("private-thread", metadata={"cwd": str(other)})
        sync(self.state)
        task, = TaskStore(state_dir=self.state).list()
        self.assertEqual(task["source"], "desktop")
        self.assertEqual(task["parent_thread_id"], "parent")
        self.assertEqual(task["forked_from_id"], "fork")
        self.assertEqual(task["title"], "Repair / unit conversion")
        relative = Path(task["path"]).relative_to(self.tasks)
        self.assertEqual(relative.parts[:2], ("codex", "desktop"))
        date = datetime.fromisoformat(task["created_at"]).astimezone().strftime("%Y-%m-%d")
        self.assertTrue(relative.name.startswith(date + "_Repair-unit-conversion_working-project_"))
        configure(self.project, self.state, enabled=False)
        self.assertFalse(status(self.state, project=self.project)["enabled"])
        with source.open("ab") as stream:
            stream.write(self.line({"type": "event_msg", "payload": {"type": "user_message", "message": "off"}}))
        self.assertEqual(sync(self.state)["records"], 0)

    def test_managed_stdout_thread_is_reused(self):
        store = TaskStore(state_dir=self.state)
        managed = store.create("codex", self.project, "managed task")
        capture = Path(managed["capture_path"])
        capture.mkdir()
        (capture / "stdout.jsonl").write_bytes(self.line({"type": "thread.started", "thread_id": "thread-a"}))
        self.write()
        sync(self.state)
        task, = store.list()
        self.assertEqual(task["task_id"], managed["task_id"])
        self.assertEqual(task["session_id"], "thread-a")

    def test_native_sidebar_title_refreshes_without_new_rollout_events(self):
        source, _ = self.write()
        sync(self.state)
        store = TaskStore(state_dir=self.state)
        original, = store.list()
        self.assertEqual(original["title"], "Codex 会话")
        captured = Path(original["capture_path"]) / "codex-rollout.jsonl"
        raw = captured.read_bytes()
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as connection:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY,name TEXT,title TEXT)")
            connection.execute("INSERT INTO threads VALUES('thread-a','新建贪吃蛇文件夹','first user request')")
            connection.commit()
        native_stat = source.stat()

        self.assertEqual(sync(self.state)["records"], 0)
        current, = store.list()
        self.assertEqual(current["title"], "新建贪吃蛇文件夹")
        self.assertEqual(current["session_title"], current["title"])
        self.assertEqual(current["native_title"], current["title"])
        self.assertEqual(current["task_id"], original["task_id"])
        self.assertEqual(current["path"], original["path"])
        self.assertEqual(captured.read_bytes(), raw)
        self.assertEqual(source.stat().st_mtime_ns, native_stat.st_mtime_ns)
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as connection:
            connection.execute("UPDATE threads SET name='贪吃蛇第二轮' WHERE id='thread-a'")
            connection.commit()
        self.assertEqual(sync(self.state)["records"], 0)
        renamed, = store.list()
        self.assertEqual(renamed["title"], "贪吃蛇第二轮")
        self.assertEqual(renamed["path"], original["path"])

    def test_native_sidebar_name_does_not_replace_managed_task_title(self):
        store = TaskStore(state_dir=self.state)
        managed = store.create("codex", self.project, "managed request", title="User supplied title")
        capture = Path(managed["capture_path"])
        capture.mkdir()
        (capture / "stdout.jsonl").write_bytes(self.line({"type": "thread.started", "thread_id": "thread-a"}))
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as connection:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY,name TEXT)")
            connection.execute("INSERT INTO threads VALUES('thread-a','Native name')")
            connection.commit()
        self.write()
        sync(self.state)
        current, = store.list()
        self.assertEqual(current["title"], "User supplied title")
        self.assertEqual(current["native_title"], "Native name")

    def test_batches_resume_without_file_mutation_and_preserve_duplicate_rows(self):
        repeated = {"type": "event_msg", "payload": {"type": "agent_message", "message": "same"}}
        source, rows = self.write(rows=[repeated, repeated, repeated])
        self.assertEqual(sync(self.state, max_records=2)["records"], 2)
        self.assertEqual(sync(self.state, max_records=2)["records"], 2)
        self.assertEqual(sync(self.state)["records"], 0)
        task, = TaskStore(state_dir=self.state).list()
        self.assertEqual((Path(task["capture_path"]) / "codex-rollout.jsonl").read_bytes(), source.read_bytes())

    def test_existing_flat_task_remains_addressable(self):
        store = TaskStore(state_dir=self.state)
        task = store.create("codex", self.project, "legacy fixture")
        path = Path(task["path"])
        old = self.tasks / task["task_id"]
        path.rename(old)
        with _edit(old) as record:
            record.update(path=str(old), capture_path=str(old / "capture"), journal_path=str(old / "journal"))
        self.assertEqual(store.path(task["task_id"]), old)
        self.assertEqual(len(store.list()), 1)

    def test_missing_derived_views_are_rebuilt_without_native_changes(self):
        source, _ = self.write(rows=[{"type": "event_msg", "payload": {"type": "user_message", "message": "Repair the log view"}}])
        sync(self.state)
        task, = TaskStore(state_dir=self.state).list()
        capture = Path(task["capture_path"])
        (capture / "codex-rollout.jsonl").unlink()
        (capture / "codex-rollout.index.jsonl").write_bytes(b"")
        result = sync(self.state)
        self.assertFalse(result["errors"])
        self.assertEqual(result["records"], 0)
        self.assertEqual((capture / "codex-rollout.jsonl").read_bytes(), source.read_bytes())
        self.assertTrue((capture / "codex-rollout.index.jsonl").stat().st_size)

    def test_task_ancestor_symlinks_are_rejected_before_writing(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.tasks / "codex").symlink_to(outside, target_is_directory=True)
        store = TaskStore(state_dir=self.state)
        with self.assertRaises(ValueError):
            store.create("codex", self.project, "must stay inside tasks")
        self.assertEqual(list(outside.iterdir()), [])

    def test_recent_thread_precedes_backlog_while_history_cursor_advances(self):
        from skill_rsi.adapters.codex import recording

        historical = []
        for number in range(20):
            source, _ = self.write(f"old-{number:02d}")
            os.utime(source, (100 + number, 100 + number))
            historical.append(source)
        recent, _ = self.write("zz-active")
        seen, clock = [], [0]
        original = recording._scan_file

        def scan(*args, **kwargs):
            seen.append(args[1])
            result = original(*args, **kwargs)
            clock[0] += 0.8
            return result

        with patch.object(recording, "_scan_file", side_effect=scan), patch.object(recording.time, "monotonic", side_effect=lambda: clock[0]):
            for _ in range(12):
                clock[0] = 0
                first = len(seen)
                sync(self.state)
                self.assertEqual(seen[first], recent)
                config = json.loads((self.state / "codex-recording.json").read_text())
                self.assertNotEqual(config.get("scan_cursor"), str(recent))
        self.assertTrue(set(historical).issubset(seen))
        self.assertEqual(len(TaskStore(state_dir=self.state).list()), 21)

    def test_native_title_falls_back_when_name_is_empty(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as connection:
            connection.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,name TEXT,title TEXT)")
            connection.execute("INSERT INTO threads VALUES('thread-a',NULL,'Native task title\nsecond line')")
            connection.commit()
        self.write(rows=[{"type": "event_msg", "payload": {"type": "user_message", "message": "injected environment"}}])
        sync(self.state)
        task, = TaskStore(state_dir=self.state).list()
        self.assertEqual(task["title"], "Native task title")

    def fresh_recording(self):
        configure(self.project, self.state, enabled=False)
        return configure(self.project, self.state, enabled=True)

    def event(self, text):
        return {"timestamp": datetime.now(timezone.utc).isoformat(), "type": "event_msg",
                "payload": {"type": "user_message", "message": text}}

    def test_new_only_existing_session_and_preexisting_partial_line(self):
        old = {"type": "event_msg", "payload": {"type": "user_message", "message": "old history"}}
        source, _ = self.write(metadata={"base_instructions": "old private instructions", "dynamic_tools": ["old tool"]}, rows=[old])
        partial = self.line(self.event("preexisting partial"))
        with source.open("ab") as stream:
            stream.write(partial[:-8])
        self.fresh_recording()
        self.assertEqual(sync(self.state)["records"], 0)
        self.assertEqual(TaskStore(state_dir=self.state).list(), [])
        self.assertEqual(status(self.state, project=self.project)["session_count"], 0)
        added = self.event("new after enable")
        with source.open("ab") as stream:
            stream.write(partial[-8:] + self.line(added))
        self.assertEqual(sync(self.state)["records"], 1)
        task, = TaskStore(state_dir=self.state).list()
        capture = Path(task["capture_path"])
        self.assertEqual((capture / "codex-rollout.jsonl").read_bytes(), self.line(added))
        context = json.loads((capture / "session-meta.json").read_text())
        self.assertTrue(context["context_only"])
        self.assertNotIn("base_instructions", context["native_session_meta"])
        self.assertNotIn("dynamic_tools", context["native_session_meta"])
        self.assertEqual(task["capture_policy"], "new_only")
        before = status(self.state, project=self.project)["recording_started_at"]
        configure(self.project, self.state, enabled=True)
        self.assertEqual(status(self.state, project=self.project)["recording_started_at"], before)
        self.assertEqual(sync(self.state)["records"], 0)

    def test_disable_reenable_archive_and_truncation_do_not_import_history(self):
        source, _ = self.write(rows=[self.event("old")])
        self.fresh_recording()
        archived = self.home / "archived_sessions" / source.name
        source.rename(archived)
        self.assertEqual(sync(self.state)["records"], 0)
        with archived.open("ab") as stream:
            stream.write(self.line(self.event("after archive")))
        self.assertEqual(sync(self.state)["records"], 1)
        configure(self.project, self.state, enabled=False)
        with archived.open("ab") as stream:
            stream.write(self.line(self.event("while disabled")))
        configure(self.project, self.state, enabled=True)
        self.assertEqual(sync(self.state)["records"], 0)
        # A rewritten generation is baselined at its current EOF.
        archived.write_bytes(self.line({"type": "session_meta", "payload": {"id": "thread-a", "cwd": str(self.project), "source": "cli"}}))
        self.assertEqual(sync(self.state)["records"], 0)
        new = self.event("after truncation")
        with archived.open("ab") as stream:
            stream.write(self.line(new))
        self.assertEqual(sync(self.state)["records"], 1)
        task, = TaskStore(state_dir=self.state).list()
        view = (Path(task["capture_path"]) / "codex-rollout.jsonl").read_bytes()
        self.assertNotIn(b"while disabled", view)
        self.assertTrue(view.endswith(self.line(new)))

    def test_subagents_and_guardians_share_visible_chat_but_user_fork_is_separate(self):
        self.write("root-chat", metadata={"source": "vscode", "originator": "Codex Desktop", "thread_source": "user"}, rows=[self.event("old root")])
        self.fresh_recording()
        now = datetime.now(timezone.utc).isoformat()
        worktree = self.root / "worktree"
        worktree.mkdir()
        for thread, extra in (("worker", {"thread_source": "subagent", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root-chat"}}}}),
                              ("guardian", {"thread_source": "guardian_review", "parent_thread_id": "root-chat", "source": {"subagent": {"other": "guardian"}}})):
            self.write(thread, metadata={"timestamp": now, "cwd": str(worktree), **extra}, rows=[self.event(thread + " result")])
        sync(self.state)
        task, = TaskStore(state_dir=self.state).list()
        self.assertEqual(task["session_id"], "root-chat")
        self.assertEqual(task["source"], "desktop")
        self.assertEqual(task["subthread_count"], 2)
        self.assertTrue(TaskStore(state_dir=self.state).detail(task["task_id"])["stdout"])
        capture = Path(task["capture_path"])
        self.assertFalse((capture / "codex-rollout.jsonl").exists())
        self.assertIn(b"worker result", (capture / "threads/worker/codex-rollout.jsonl").read_bytes())
        copied = {"timestamp": "2020-01-01T00:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "copied history"}}
        self.write("user-fork", metadata={"timestamp": now, "thread_source": "user", "forked_from_id": "root-chat"}, rows=[copied, self.event("new fork input")])
        sync(self.state)
        self.assertEqual(status(self.state, project=self.project)["session_count"], 2)
        fork = next(row for row in TaskStore(state_dir=self.state).list() if row["session_id"] == "user-fork")
        self.assertNotIn(b"copied history", (Path(fork["capture_path"]) / "codex-rollout.jsonl").read_bytes())

    def test_rewrite_between_enable_and_first_scan_resets_the_baseline(self):
        source, _ = self.write(rows=[self.event("pre-enable")])
        self.fresh_recording()
        # Keep the inode and replace the prefix with a longer old generation.
        source.write_bytes(self.line({"type": "session_meta", "payload": {"id": "thread-a", "cwd": str(self.project)}})
                           + self.line({"timestamp": "2020-01-01T00:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "old " * 500}}))
        result = sync(self.state)
        self.assertEqual(result["records"], 0)
        self.assertEqual(TaskStore(state_dir=self.state).list(), [])
        current = self.event("after rewritten baseline")
        with source.open("ab") as stream:
            stream.write(self.line(current))
        self.assertEqual(sync(self.state)["records"], 1)
        task, = TaskStore(state_dir=self.state).list()
        self.assertEqual((Path(task["capture_path"]) / "codex-rollout.jsonl").read_bytes(), self.line(current))

    def test_missing_parent_file_cannot_create_a_task_from_cached_metadata(self):
        parent, _ = self.write("removed-root", metadata={"thread_source": "user"})
        self.fresh_recording()
        sync(self.state)
        parent.unlink()
        self.write("orphan", metadata={"timestamp": datetime.now(timezone.utc).isoformat(), "thread_source": "subagent",
                                       "source": {"subagent": {"thread_spawn": {"parent_thread_id": "removed-root"}}}},
                   rows=[self.event("orphan result")])
        self.assertEqual(sync(self.state)["session_count"], 0)
        self.assertEqual(TaskStore(state_dir=self.state).list(), [])


if __name__ == "__main__":
    unittest.main()
