"""Installed UI defaults and artifact locations are independent of the old teacher."""

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from skill_rsi.components.description import describe
from skill_rsi.discovery.codex_settings import get_settings, save_settings
from skill_rsi.ui.service import Manager


class GenerationInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.manager = Manager(self.project, self.root / "state", self.root / "tasks")

    def test_default_analysis_starts_codex_generation_with_saved_preferences(self):
        task = self.manager.tasks.create("codex", self.project, "synthetic task")
        self.assertEqual(get_settings(self.manager.state_dir)["model"], "gpt-6.1-sol")
        save_settings(self.manager.state_dir, {"effort": "max"})
        with patch("skill_rsi.emergence.start", return_value={"status": "running"}) as start:
            self.manager.analyze({"task_id": task["task_id"], "instructions": "Read all streams"})
        self.assertEqual(start.call_args.kwargs["model"], "gpt-6.1-sol")
        self.assertEqual(start.call_args.kwargs["effort"], "max")
        self.assertEqual(start.call_args.kwargs["instructions"], "Read all streams")

    def test_invalid_preferences_do_not_replace_saved_model(self):
        state = self.manager.state_dir
        save_settings(state, {"model": "custom-model", "effort": "ultra"})
        with self.assertRaises(ValueError):
            save_settings(state, {"model": "changed", "effort": "bogus"})
        self.assertEqual(get_settings(state)["model"], "custom-model")

    def test_workbench_start_recovers_finalizers_once(self):
        with patch("skill_rsi.adapters.codex.recording.watch") as watch, \
             patch("skill_rsi.emergence.recover") as recover:
            self.manager.start_recording()
            self.manager.start_recording()
            self.manager._recovery_thread.join(timeout=5)
            self.manager.close()
        watch.assert_called_once()
        recover.assert_called_once_with(self.manager.tasks, state_dir=self.manager.state_dir)

    def test_package_description_reads_review_without_loading_payload(self):
        source = self.root / "candidate"
        source.mkdir()
        (source / "policy.py").write_text("raise AssertionError('must never import')")
        (source / "README.md").write_text("A full human readable description")
        (source / "review.json").write_text(json.dumps({"display_name": "Check evidence", "category": "Versions",
                                                        "use_when": ["resuming"], "skip_when": ["fresh evidence"]}))
        metadata = describe("synthetic-component", source)
        self.assertEqual(metadata["use_when"], ["resuming"])
        self.assertEqual(metadata["storage_path"], str(source))
        self.assertIn("human readable", metadata["readme"])

    def test_open_location_reveals_registered_file_and_rejects_outside_path(self):
        task = self.manager.tasks.create("codex", self.project, "location check")
        with patch("skill_rsi.ui.service.subprocess.Popen") as launch:
            self.manager.open_path({"path": task["path"]})
            self.assertEqual(launch.call_count, 1)
            with self.assertRaises(ValueError):
                self.manager.open_path({"path": str(self.project)})
            self.assertEqual(launch.call_count, 1)

    def test_legacy_skill_uses_its_authored_trigger_and_skip_sections(self):
        root = self.root / "legacy"
        (root / "payload").mkdir(parents=True)
        (root / "manifest.json").write_text(json.dumps({"form": "skill", "entrypoint": "payload/SKILL.md"}))
        (root / "payload/SKILL.md").write_text("# Check active context\n\n## 触发条件\nResume a task.\n\n## 跳过条件\nCurrent evidence is already valid.\n")
        row = describe("legacy", root)
        self.assertEqual(row["display_name"], "Check active context")
        self.assertEqual(row["use_when"], ["Resume a task."])
        self.assertEqual(row["skip_when"], ["Current evidence is already valid."])


if __name__ == "__main__":
    unittest.main()
