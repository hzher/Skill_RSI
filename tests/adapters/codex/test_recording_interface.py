"""The same native recording contract is exposed by the UI and installed CLI."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skill_rsi.cli import main
from skill_rsi.ui.service import Manager


class RecordingInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.project = root / "project"
        self.project.mkdir()
        self.home = root / "codex"
        self.state = root / "state"
        self.tasks = root / "tasks"
        sessions = self.home / "sessions" / "2026" / "11" / "25"
        sessions.mkdir(parents=True)
        for thread, source, originator in (("cli-thread", "cli", "codex_cli_rs"),
                                            ("desktop-thread", "vscode", "Codex Desktop")):
            rows = [
                {"type": "session_meta", "payload": {"id": thread, "cwd": str(self.project),
                                                       "source": source, "originator": originator}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "Inspect a small example"}},
                {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Done"}},
            ]
            (sessions / f"rollout-{thread}.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def append_new(self):
        for source in (self.home / "sessions").rglob("*.jsonl"):
            with source.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "event_msg", "payload": {
                    "type": "user_message", "message": "New visible request"}}) + "\n")

    def test_ui_enable_sync_and_stop_preserve_tasks(self):
        manager = Manager(self.project, self.state, self.tasks)
        manager.codex_recording({"project": str(self.project), "enabled": True, "codex_home": str(self.home)})
        manager.codex_sync({"project": str(self.project)})
        self.assertEqual(manager.tasks.list(), [])
        self.append_new()
        manager.codex_sync({"project": str(self.project)})
        tasks = manager.tasks.list()
        self.assertEqual(len(tasks), 2)
        self.assertEqual({task["session_id"] for task in tasks}, {"cli-thread", "desktop-thread"})
        self.assertEqual({Path(task["path"]).relative_to(self.tasks).parts[:2] for task in tasks},
                         {("codex", "cli"), ("codex", "desktop")})
        for task in tasks:
            detail = manager.task(task["task_id"])
            self.assertTrue(detail["events"])
            self.assertIn("New visible request", detail["stdout"])
            self.assertNotIn("Inspect a small example", detail["stdout"])
        manager.codex_recording({"project": str(self.project), "enabled": False})
        manager.codex_sync({"project": str(self.project)})
        self.assertEqual(len(manager.tasks.list()), 2)
        with self.assertRaisesRegex(ValueError, "enabled"):
            manager.codex_recording({"project": str(self.project), "enabled": "false"})

    def test_cli_once_status_disable_share_native_task_identity(self):
        prefix = ["--state-dir", str(self.state), "record", "--host", "codex", "--project", str(self.project)]
        for index, flags in enumerate((["--once", "--codex-home", str(self.home), "--tasks-dir", str(self.tasks)],
                                       ["--once"], ["--status"], ["--disable"])):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(prefix + flags), 0)
            self.assertIsInstance(json.loads(output.getvalue()), dict)
            if index == 0:
                self.assertEqual(Manager(self.project, self.state).tasks.list(), [])
                self.append_new()
        manager = Manager(self.project, self.state)
        self.assertEqual(len(manager.tasks.list()), 2)

    def test_model_analysis_launch_is_not_a_user_conversation(self):
        manager = Manager(self.project, self.state, self.tasks)
        with patch("skill_rsi.tasks.store.subprocess.Popen") as launch, \
                patch("skill_rsi.tasks.store._process_stamp", return_value=None):
            launch.return_value.pid = 999999
            job = manager.tasks.start("codex", self.project, "Analyze synthetic evidence", mode="llm_analysis")
        self.assertEqual(job["mode"], "llm_analysis")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(Path(job["path"]).relative_to(self.tasks).parts[:2], ("codex", "analysis"))


if __name__ == "__main__":
    unittest.main()
