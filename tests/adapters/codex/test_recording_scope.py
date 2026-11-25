"""Regression boundaries for global new-only capture and one-chat history import."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from skill_rsi.adapters.codex import recording
from skill_rsi.tasks.store import TaskStore


class RecordingScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "codex"
        (self.home / "sessions").mkdir(parents=True)
        (self.home / "archived_sessions").mkdir()
        self.state = self.root / "state"
        self.tasks = self.root / "tasks"

    @staticmethod
    def line(value):
        return (json.dumps(value, ensure_ascii=False) + "\n").encode()

    @staticmethod
    def event(text, *, historical=False):
        return {"timestamp": "2020-01-01T00:00:00Z" if historical else datetime.now(timezone.utc).isoformat(),
                "type": "event_msg", "payload": {"type": "user_message", "message": text}}

    def source(self, thread, *, project="project-a", metadata=None, historical=True, events=None):
        work = self.root / project
        work.mkdir(parents=True, exist_ok=True)
        header = {"id": thread, "cwd": str(work), "source": "vscode", "originator": "Codex Desktop",
                  "thread_source": "user", "timestamp": "2020-01-01T00:00:00Z" if historical else datetime.now(timezone.utc).isoformat(),
                  **(metadata or {})}
        rows = [{"type": "session_meta", "payload": header}, *(events or [])]
        path = self.home / "sessions" / f"rollout-{thread}.jsonl"
        path.write_bytes(b"".join(self.line(row) for row in rows))
        return path

    def enable(self, **options):
        return recording.configure(None, self.state, scope="all_projects", codex_home=self.home,
                                   tasks_dir=self.tasks, **options)

    def append(self, path, event):
        with path.open("ab") as stream:
            stream.write(self.line(event))

    def all_tasks(self):
        return TaskStore(state_dir=self.state).list()

    def recorded(self):
        path = self.state / "codex-recording.sqlite3"
        if not path.is_file():
            return []
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM threads ORDER BY thread")]

    def assert_capture(self, task, relative_path, expected):
        capture = Path(task["path"]) / relative_path
        actual = capture.read_bytes()
        self.assertEqual(actual, expected)
        index = capture.with_name("codex-rollout.index.jsonl")
        origins = [json.loads(line) for line in index.read_bytes().splitlines()]
        raws = actual.splitlines(keepends=True)
        self.assertEqual(len(origins), len(raws))
        for origin, raw in zip(origins, raws):
            self.assertEqual(origin["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(origin["bytes"], len(raw))
            self.assertEqual((capture.parent / origin["record_path"]).read_bytes(), raw)

    def family(self):
        root = self.source("visible-chat", events=[self.event("root history", historical=True)])
        worker = self.source("worker", project="worker-worktree", metadata={
            "thread_source": "subagent", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "visible-chat"}}}},
            events=[self.event("worker history", historical=True)])
        guardian = self.source("guardian", metadata={"thread_source": "guardian_review", "parent_thread_id": "worker",
                                                     "source": {"subagent": {"other": "guardian"}}},
                               events=[self.event("guardian history", historical=True)])
        self.source("user-fork", metadata={"forked_from_id": "visible-chat"},
                    events=[self.event("fork history", historical=True)])
        self.source("unrelated", project="other-project", events=[self.event("unrelated history", historical=True)])
        self.source("orphan", metadata={"thread_source": "subagent", "parent_thread_id": "absent-root",
                                        "source": {"subagent": {"other": "guardian"}}})
        for child, parent in (("cycle-a", "cycle-b"), ("cycle-b", "cycle-a")):
            self.source(child, metadata={"thread_source": "subagent", "parent_thread_id": parent,
                                         "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}})
        return {"visible-chat": root, "worker": worker, "guardian": guardian}

    def test_global_baseline_and_unregistered_projects_capture_only_new_rows(self):
        original = self.source("existing", events=[self.event("old activity", historical=True)])
        self.source("unrelated-old", project="never-enabled", events=[self.event("do not backfill", historical=True)])
        self.enable()
        self.assertEqual(recording.sync(self.state)["records"], 0)
        self.assertEqual(self.all_tasks(), [])

        added = self.event("new activity in existing unknown project")
        self.append(original, added)
        self.assertEqual(recording.sync(self.state)["records"], 1)
        task, = self.all_tasks()
        self.assertEqual(task["project"], str(self.root / "project-a"))
        self.assert_capture(task, "capture/codex-rollout.jsonl", self.line(added))

        fresh = self.event("new project and new user chat")
        self.source("new-chat", project="new-project", historical=False, events=[fresh])
        self.assertEqual(recording.sync(self.state)["records"], 1)
        self.assertEqual({row["session_id"] for row in self.all_tasks()}, {"existing", "new-chat"})
        self.assertNotIn("unrelated-old", {row["thread"] for row in self.recorded()})

    def test_repeated_enable_and_reopened_store_do_not_reset_the_cursor(self):
        source = self.source("persistent", events=[self.event("old", historical=True)])
        self.enable()
        recording.sync(self.state)
        first = self.event("pending before repeated enable")
        self.append(source, first)
        self.enable()
        self.assertEqual(recording.sync(self.state)["records"], 1)
        second = self.event("after store reopens")
        self.append(source, second)
        restarted = subprocess.run([sys.executable, "-c",
                                    "import json,sys; from skill_rsi.adapters.codex.recording import sync; print(json.dumps(sync(sys.argv[1])))",
                                    str(self.state)], check=True, capture_output=True, text=True, timeout=15)
        self.assertEqual(json.loads(restarted.stdout)["records"], 1)
        self.assertEqual(recording.sync(self.state)["records"], 0)
        task, = self.all_tasks()
        self.assert_capture(task, "capture/codex-rollout.jsonl", self.line(first) + self.line(second))

    def test_new_chat_waits_for_async_title_without_advancing_event_cursor(self):
        self.enable()
        context = {"timestamp": datetime.now(timezone.utc).isoformat(), "type": "turn_context", "payload": {"cwd": str(self.root / "project-a")}}
        source = self.source("awaiting-title", historical=False, events=[context])
        first = recording.sync(self.state)
        self.assertEqual(first["records"], 0)
        self.assertTrue(first["pending"])
        self.assertEqual(self.all_tasks(), [])
        with closing(sqlite3.connect(self.state / "codex-recording.sqlite3")) as connection:
            offset, line = connection.execute("SELECT offset,line FROM streams WHERE thread='awaiting-title'").fetchone()
        self.assertEqual(offset, len(source.read_bytes().splitlines(keepends=True)[0]))
        self.assertEqual(line, 1)
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as connection:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY,name TEXT)")
            connection.execute("INSERT INTO threads VALUES('awaiting-title','新建贪吃蛇文件夹')")
            connection.commit()
        second = recording.sync(self.state)
        self.assertEqual(second["records"], 1)
        self.assertFalse(second["pending"])
        task, = self.all_tasks()
        self.assertEqual(task["title"], "新建贪吃蛇文件夹")
        self.assertIn("_新建贪吃蛇文件夹_project-a_", Path(task["path"]).name)
        self.assert_capture(task, "capture/codex-rollout.jsonl", self.line(context))
        self.assertEqual(recording.sync(self.state)["records"], 0)

    def test_pending_chat_uses_later_first_user_message_and_keeps_earlier_new_events(self):
        self.enable()
        context = {"timestamp": datetime.now(timezone.utc).isoformat(), "type": "turn_context", "payload": {}}
        source = self.source("awaiting-input", historical=False, events=[context])
        self.assertTrue(recording.sync(self.state)["pending"])
        message = self.event("Build a snake game")
        self.append(source, message)
        self.assertEqual(recording.sync(self.state)["records"], 2)
        task, = self.all_tasks()
        self.assertEqual(task["title"], "Build a snake game")
        self.assert_capture(task, "capture/codex-rollout.jsonl", self.line(context) + self.line(message))

    def test_disable_and_reenable_skip_activity_while_disabled(self):
        source = self.source("pause", events=[self.event("old", historical=True)])
        self.enable()
        before = self.event("before disabling")
        self.append(source, before)
        self.assertEqual(recording.sync(self.state)["records"], 1)
        self.enable(enabled=False)
        self.append(source, self.event("must not import disabled interval"))
        self.assertEqual(recording.sync(self.state)["records"], 0)
        self.enable()
        self.assertEqual(recording.sync(self.state)["records"], 0)
        after = self.event("after reenable")
        self.append(source, after)
        self.assertEqual(recording.sync(self.state)["records"], 1)
        task, = self.all_tasks()
        self.assert_capture(task, "capture/codex-rollout.jsonl", self.line(before) + self.line(after))

    def test_global_partial_baseline_archive_move_and_rewrite_skip_history(self):
        source = self.source("moving", events=[self.event("old", historical=True)])
        partial = self.line(self.event("started before enable", historical=True))
        with source.open("ab") as stream:
            stream.write(partial[:-5])
        self.enable()
        archived = self.home / "archived_sessions" / source.name
        source.rename(archived)
        added = self.event("new after archive")
        with archived.open("ab") as stream:
            stream.write(partial[-5:] + self.line(added))
        self.assertEqual(recording.sync(self.state)["records"], 1)
        task, = self.all_tasks()
        self.assert_capture(task, "capture/codex-rollout.jsonl", self.line(added))
        rewritten = self.line({"type": "session_meta", "payload": {"id": "moving", "cwd": str(self.root / "project-a"),
                                                                    "thread_source": "user", "source": "vscode"}})
        archived.write_bytes(rewritten + self.line(self.event("rewritten old history", historical=True)))
        self.assertEqual(recording.sync(self.state)["records"], 0)
        new = self.event("new after rewrite")
        self.append(archived, new)
        self.assertEqual(recording.sync(self.state)["records"], 1)
        self.assert_capture(task, "capture/codex-rollout.jsonl", self.line(added) + self.line(new))

    def test_import_is_one_proven_family_and_preserves_raw_indexes(self):
        family = self.family()
        self.enable()
        self.assertEqual(recording.sync(self.state)["records"], 0)
        imported = recording.import_session("visible-chat", self.state)
        self.assertEqual(set(imported["family"]), set(family))
        self.assertEqual(imported["errors"], [])
        self.assertEqual(imported["records"], 6)
        task, = self.all_tasks()
        self.assertEqual(imported["task_id"], task["task_id"])
        self.assertEqual(task["session_id"], "visible-chat")
        self.assertEqual(task["subthread_count"], 2)
        self.assertEqual({row["thread"] for row in self.recorded()}, set(family))
        for thread, source in family.items():
            relative = "capture/codex-rollout.jsonl" if thread == "visible-chat" else f"capture/threads/{thread}/codex-rollout.jsonl"
            self.assert_capture(task, relative, source.read_bytes())
        self.assertEqual(recording.sync(self.state)["records"], 0)
        again = recording.import_session("visible-chat", self.state)
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["records"], 0)
        self.assertEqual(len(self.all_tasks()), 1)

    def test_import_does_not_reset_global_baseline_or_backfill_other_chats(self):
        family = self.family()
        self.enable()
        imported = recording.import_session("visible-chat", self.state)
        unrelated = self.home / "sessions/rollout-unrelated.jsonl"
        new = self.event("new unrelated activity after specific import")
        self.append(unrelated, new)
        self.assertEqual(recording.sync(self.state)["records"], 1)
        tasks = self.all_tasks()
        self.assertEqual(len(tasks), 2)
        extra = next(row for row in tasks if row["session_id"] == "unrelated")
        self.assert_capture(extra, "capture/codex-rollout.jsonl", self.line(new))
        self.assertEqual({row["thread"] for row in self.recorded()}, {*family, "unrelated"})
        self.assertEqual(next(row for row in tasks if row["task_id"] == imported["task_id"])["subthread_count"], 2)

    def test_import_restores_native_order_after_live_rows_were_captured(self):
        source = self.source("already-live", events=[self.event("old constraint", historical=True)])
        self.enable()
        fresh = self.event("new correction supersedes old constraint")
        self.append(source, fresh)
        self.assertEqual(recording.sync(self.state)["records"], 1)
        task, = self.all_tasks()
        self.assert_capture(task, "capture/codex-rollout.jsonl", self.line(fresh))
        result = recording.import_session("already-live", self.state)
        self.assertEqual(result["records"], 2)
        self.assert_capture(task, "capture/codex-rollout.jsonl", source.read_bytes())
        self.assertEqual(recording.sync(self.state)["records"], 0)
        self.assertEqual(recording.import_session("already-live", self.state)["records"], 0)
        self.assert_capture(task, "capture/codex-rollout.jsonl", source.read_bytes())

    def test_child_import_and_global_history_capture_are_rejected(self):
        self.family()
        self.enable()
        with self.assertRaises(ValueError):
            recording.import_session("worker", self.state)
        self.assertEqual(self.all_tasks(), [])
        with self.assertRaises(ValueError):
            self.enable(capture_existing=True)
        self.assertEqual(self.all_tasks(), [])

    def test_global_user_fork_stays_separate_and_backgrounds_share_root(self):
        self.source("visible-chat", events=[self.event("old root", historical=True)])
        self.enable()
        now = datetime.now(timezone.utc).isoformat()
        self.source("worker", project="new-worktree", historical=False, metadata={
            "thread_source": "subagent", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "visible-chat"}}}},
            events=[self.event("new worker output")])
        self.source("guardian", historical=False, metadata={"thread_source": "guardian_review", "parent_thread_id": "worker",
                                                            "source": {"subagent": {"other": "guardian"}}},
                    events=[self.event("new guardian activity")])
        self.source("user-fork", historical=False, metadata={"thread_source": "user", "forked_from_id": "visible-chat",
                                                             "timestamp": now},
                    events=[self.event("copied old input", historical=True), self.event("new fork input")])
        result = recording.sync(self.state)
        tasks = self.all_tasks()
        self.assertEqual(result["session_count"], 2)
        self.assertEqual(result["threads"], 2)
        self.assertEqual({row["session_id"] for row in tasks}, {"visible-chat", "user-fork"})
        parent = next(row for row in tasks if row["session_id"] == "visible-chat")
        self.assertEqual(parent["subthread_count"], 2)
        self.assertFalse((Path(parent["path"]) / "capture/codex-rollout.jsonl").exists())
        self.assertTrue(TaskStore(state_dir=self.state).detail(parent["task_id"])["stdout"])
        fork = next(row for row in tasks if row["session_id"] == "user-fork")
        self.assertNotIn(b"copied old input", (Path(fork["path"]) / "capture/codex-rollout.jsonl").read_bytes())

    def test_global_disable_cannot_fall_back_to_legacy_project_recording(self):
        source = self.source("legacy", events=[self.event("legacy history", historical=True)])
        project = self.root / "project-a"
        recording.configure(project, self.state, codex_home=self.home, tasks_dir=self.tasks)
        self.enable()
        self.enable(enabled=False)
        self.append(source, self.event("activity while global recording is disabled"))
        self.assertEqual(recording.sync(self.state)["records"], 0)
        self.assertEqual(recording.status(self.state, project=project)["phase"], "disabled")
        recording.configure(project, self.state, enabled=True)
        implicit = recording.status(self.state, project=project)
        self.assertEqual(implicit["scope"], "all_projects")
        self.assertFalse(implicit["enabled"])
        self.assertEqual(recording.sync(self.state)["records"], 0)
        recording.configure(project, self.state, enabled=True, scope="project")
        explicit = recording.status(self.state, project=project)
        self.assertEqual(explicit["scope"], "project")
        self.assertTrue(explicit["enabled"])
        self.assertEqual(recording.sync(self.state)["records"], 0)
        after = self.event("new activity after explicitly resuming project recording")
        self.append(source, after)
        self.assertEqual(recording.sync(self.state)["records"], 1)
        task, = self.all_tasks()
        self.assert_capture(task, "capture/codex-rollout.jsonl", self.line(after))

    def test_global_status_is_the_same_from_each_project_context(self):
        a = self.source("chat-a", project="project-a", events=[self.event("old a", historical=True)])
        b = self.source("chat-b", project="project-b", events=[self.event("old b", historical=True)])
        self.enable()
        self.append(a, self.event("new a"))
        self.append(b, self.event("new b"))
        synced = recording.sync(self.state, project=self.root / "project-a")
        self.assertEqual(synced["session_count"], 2)
        self.assertEqual({row["session_id"] for row in self.all_tasks()}, {"chat-a", "chat-b"})
        contexts = [recording.status(self.state, project=self.root / name) for name in ("project-a", "project-b")]
        self.assertEqual({row["scope"] for row in contexts}, {"all_projects"})
        for key in ("session_count", "threads", "last_sync", "recording_started_at", "phase"):
            self.assertEqual(contexts[0][key], contexts[1][key], key)
        self.assertEqual(contexts[0]["session_count"], 2)

    def test_native_analysis_job_is_reused_without_counting_a_user_chat(self):
        self.enable()
        project = self.root / "analysis-workspace"
        project.mkdir()
        store = TaskStore(state_dir=self.state)
        job = store.create("codex", project, "Analyze synthetic task evidence", mode="llm_analysis")
        capture = Path(job["capture_path"])
        capture.mkdir()
        (capture / "stdout.jsonl").write_bytes(self.line({"type": "thread.started", "thread_id": "analysis-thread"}))
        self.source("analysis-thread", project="analysis-workspace", historical=False,
                    metadata={"source": "exec", "thread_source": "user"}, events=[self.event("analysis result")])
        synced = recording.sync(self.state)
        tasks = self.all_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_id"], job["task_id"])
        self.assertEqual(tasks[0]["mode"], "llm_analysis")
        self.assertEqual(self.recorded()[0]["task_id"], job["task_id"])
        self.assertEqual(synced["session_count"], 0)
        self.assertEqual(synced["threads"], 0)
        self.assertEqual(synced["native_thread_count"], 1)
        current = recording.status(self.state)
        self.assertEqual(current["session_count"], 0)
        self.assertEqual(current["threads"], 0)


if __name__ == "__main__":
    unittest.main()
