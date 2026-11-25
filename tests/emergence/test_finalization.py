"""Lifecycle regressions using synthetic evidence and local, non-model workers."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from skill_rsi.adapters.codex import Adapter as CodexAdapter
from skill_rsi.adapters.dsh import Adapter as DshAdapter
from skill_rsi.emergence import workflow
from skill_rsi.evidence.io import read_json, write_json
from skill_rsi.tasks.store import TaskStore


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.project = self.root / "project"
        self.project.mkdir()
        self.tasks = TaskStore(self.root / "tasks", state_dir=self.state)
        source = self.tasks.create("codex", self.project, title="synthetic source", mode="observed", source="desktop")
        self.task = Path(source["path"])
        capture = self.task / "capture"
        capture.mkdir()
        (capture / "codex-rollout.jsonl").write_text(json.dumps({
            "type": "event_msg", "payload": {"type": "user_message", "message": "Synthetic visible evidence."}
        }) + "\n")
        self.launcher = patch.object(workflow, "_launch_finalizer", return_value=False)
        self.launch = self.launcher.start()
        self.addCleanup(self.launcher.stop)
        self.starter = patch.object(self.tasks, "start", side_effect=self.create_worker)
        self.start_worker = self.starter.start()
        self.addCleanup(self.starter.stop)

    def create_worker(self, host, workspace, prompt, *, title, mode, options):
        return self.tasks.create(host, workspace, prompt, title=title, mode=mode)

    def begin(self):
        return workflow.start(self.task, self.tasks, self.state, command="/synthetic/codex")

    def set_worker(self, job, status="completed", **fields):
        path = self.tasks.path(job["analysis_job_id"]) / "task.json"
        record = read_json(path)
        record.update(status=status, **fields)
        write_json(path, record)
        return record

    def candidate(self, job):
        root = Path(job["components_path"]) / "synthetic-candidate"
        (root / "payload").mkdir(parents=True)
        (root / "payload" / "SKILL.md").write_text(
            "---\nname: synthetic-candidate\ndescription: Synthetic lifecycle fixture\n---\n\nCheck existing evidence before continuing.\n")
        write_json(root / "manifest.json", {
            "schema": "skill-rsi.component.v1", "component_id": "synthetic-candidate", "version": "1.0.0",
            "form": "skill", "engine": "skill", "entrypoint": "payload/SKILL.md",
            "scope": {"hosts": ["codex"], "modes": ["shadow"]},
            "trigger": {"boundaries": ["before_action"]},
            "action": {"kind": "append_context", "target": "context.synthetic", "writes": []},
            "required_capabilities": [], "budget": {"max_action_bytes": 8192}, "fallback": "native",
        })
        (root / "README.md").write_text("# Synthetic candidate\n\nFor lifecycle checks only. No effectiveness claim.\n")
        write_json(root / "review.json", {
            "display_name": "Synthetic candidate", "description": "A synthetic lifecycle fixture.",
            "category": "Testing", "use_when": ["Running lifecycle checks"], "skip_when": ["Doing real tasks"],
            "mechanism": "Retain evidence", "limitations": ["Synthetic only"], "evaluation_plan": ["Independent review"],
        })
        Path(job["report_path"]).write_text("Synthetic report. No runtime or effectiveness verification.\n")
        return root

    def test_start_launches_finalizer_with_exact_job_and_store(self):
        job = self.begin()
        self.launch.assert_called_once_with(self.task, job["job_id"], self.tasks, self.state)
        self.assertFalse(job.get("finalized_at"))

    def test_supervisor_finishes_without_ui_polling(self):
        from skill_rsi.emergence.supervisor import supervise
        job = self.begin()
        self.candidate(job)
        self.set_worker(job, returncode=0)
        result = supervise(self.task, job["job_id"], self.tasks, interval=0.01)
        saved = read_json(Path(job["output_path"]) / "job.json")
        self.assertEqual(saved["status"], "completed")
        self.assertTrue(saved["finalized_at"])
        self.assertEqual(saved["valid_candidate_count"], 1)
        self.assertTrue(Path(saved["candidates"][0]["archive_path"]).is_file())
        self.assertEqual(result["job_id"], job["job_id"])
        self.start_worker.assert_called_once()

    def test_supervisor_keeps_worker_failure_and_does_not_publish_candidate(self):
        from skill_rsi.emergence.supervisor import supervise
        job = self.begin()
        self.candidate(job)
        self.set_worker(job, "failed", returncode=23, error="synthetic worker failure")
        result = supervise(self.task, job["job_id"], self.tasks, interval=0.01)
        self.assertEqual(result["status"], "failed")
        self.assertIn("synthetic worker failure", result["error"])
        self.assertEqual(result["candidates"], [])
        self.assertFalse(list(Path(job["output_path"]).rglob("*.zip")))
        self.assertTrue(result["finalized_at"])

    def test_old_finalizer_does_not_replace_new_current_job(self):
        from skill_rsi.emergence.supervisor import supervise
        older = self.begin()
        self.candidate(older)
        self.set_worker(older, returncode=0)
        newer = self.begin()
        self.assertNotEqual(older["job_id"], newer["job_id"])
        result = supervise(self.task, older["job_id"], self.tasks, interval=0.01)
        current = read_json(self.task / "emergence.json")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(current["job_id"], newer["job_id"])
        self.assertEqual(current["status"], "running")
        self.assertFalse(current.get("finalized_at"))
        self.assertEqual(read_json(Path(older["output_path"]) / "job.json")["status"], "completed")

    def test_ui_poll_and_supervisor_only_pack_once(self):
        from skill_rsi.emergence.check import validate
        from skill_rsi.emergence.supervisor import supervise
        job = self.begin()
        self.candidate(job)
        self.set_worker(job, returncode=0)
        barrier = threading.Barrier(2)

        def run(kind):
            barrier.wait(timeout=3)
            if kind == "supervisor":
                return supervise(self.task, job["job_id"], self.tasks, interval=0.01)
            return workflow.poll(self.task, self.tasks)

        with patch("skill_rsi.emergence.check.validate", wraps=validate) as validation:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(run, ("supervisor", "ui")))
        self.assertEqual(validation.call_count, 1)
        self.assertTrue(all(result["status"] == "completed" for result in results))
        self.assertEqual(len(list((Path(job["output_path"]) / "artifacts").glob("*.zip"))), 1)

    def test_recover_only_resumes_pending_finalizers_without_new_model(self):
        pending = self.begin()
        self.set_worker(pending, returncode=0)
        current = self.begin()
        self.launch.reset_mock()
        starts = self.start_worker.call_count
        with patch.object(workflow, "_launch_finalizer", return_value=True) as launch:
            result = workflow.recover(self.tasks, self.state)
        resumed = {call.args[1] for call in launch.call_args_list}
        self.assertEqual(resumed, {pending["job_id"], current["job_id"]})
        self.assertEqual(result["resumed"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(self.start_worker.call_count, starts)
        workflow.poll(self.task, self.tasks, job_id=pending["job_id"])
        self.set_worker(current, returncode=0)
        workflow.poll(self.task, self.tasks)
        with patch.object(workflow, "_launch_finalizer", return_value=True) as launch:
            again = workflow.recover(self.tasks, self.state)
        launch.assert_not_called()
        self.assertEqual(again["resumed"], 0)
        self.assertEqual(self.start_worker.call_count, starts)

    def test_finalizer_launch_failure_leaves_generation_recoverable(self):
        with patch.object(workflow, "_launch_finalizer", side_effect=OSError("synthetic spawn failure")):
            job = self.begin()
        self.assertEqual(job["status"], "running")
        persisted = read_json(Path(job["output_path"]) / "job.json")
        self.assertIn("synthetic spawn failure", persisted["finalizer_error"])
        self.set_worker(job, returncode=0)
        self.assertEqual(workflow.poll(self.task, self.tasks)["status"], "completed")

    def test_generation_python_is_explicit_and_scoped_to_codex(self):
        job = self.begin()
        options = self.start_worker.call_args.kwargs["options"]
        self.assertEqual(options["python_executable"], sys.executable)
        guide = (Path(job["sdk_path"]) / "SDK.md").read_text()
        prompt = (Path(job["output_path"]) / "prompt.txt").read_text()
        environment = read_json(Path(job["sdk_path"]) / "environment.json")
        self.assertEqual(environment["python_executable"], sys.executable)
        self.assertIn(sys.executable, guide)
        self.assertIn(sys.executable, prompt)
        with patch.dict(os.environ, {"PATH": "/synthetic/system/bin"}):
            codex = CodexAdapter().environment(self.root, options)
            dsh = DshAdapter().environment(self.root, options)
            self.assertEqual(codex["PATH"].split(os.pathsep)[0], str(Path(sys.executable).parent))
            self.assertNotIn(str(Path(sys.executable).parent), dsh["PATH"].split(os.pathsep))
            self.assertEqual(os.environ["PATH"], "/synthetic/system/bin")

    def test_recovered_command_failure_is_activity_not_generation_failure(self):
        job = self.begin()
        capture = self.tasks.path(job["analysis_job_id"]) / "capture"
        capture.mkdir()
        command = {"type": "command_execution", "id": "command-one", "command": "synthetic-missing-python"}
        events = [
            {"type": "item.started", "item": {**command, "status": "in_progress"}},
            {"type": "item.completed", "item": {**command, "status": "failed", "exit_code": 127}},
            {"type": "item.completed", "item": {**command, "status": "failed", "exit_code": 127}},
            {"type": "item.completed", "item": {**command, "id": "command-two", "command": "synthetic-recovery", "status": "completed", "exit_code": 0}},
        ]
        (capture / "stdout.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
        (capture / "stderr.log").write_text("failed to refresh available models: request timed out\n")
        running = workflow.poll(self.task, self.tasks)
        self.assertEqual(running["phase"], "running")
        self.set_worker(job, returncode=0)
        completed_worker = self.tasks.get(job["analysis_job_id"])
        finalizing = workflow._present(self.task, read_json(Path(job["output_path"]) / "job.json"), completed_worker)
        self.assertEqual(finalizing["phase"], "finalizing")
        self.assertTrue(finalizing["worker_completed"])
        finished = workflow.poll(self.task, self.tasks)
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["phase"], "completed")
        self.assertEqual(finished["activity"]["commands_completed"], 2)
        self.assertEqual(finished["activity"]["commands_nonzero"], 1)
        self.assertEqual(finished["activity"]["scope"], "complete")
        self.assertTrue(finished["activity"]["latest_command_failure"])
        self.assertTrue(finished["activity"]["warnings"])

    def test_truncated_command_scan_does_not_claim_complete_totals(self):
        job = self.begin()
        capture = self.tasks.path(job["analysis_job_id"]) / "capture"
        capture.mkdir()
        huge = {"type": "item.completed", "item": {"type": "command_execution", "id": "large",
                "command": "synthetic-oversized", "exit_code": 0, "aggregated_output": "x" * (3 * 1024 * 1024)}}
        recent = {"type": "item.completed", "item": {"type": "command_execution", "id": "recent",
                  "command": "synthetic-recent", "status": "completed", "exit_code": 0}}
        (capture / "stdout.jsonl").write_text(json.dumps(huge) + "\n" + json.dumps(recent) + "\n")
        result = workflow.poll(self.task, self.tasks)
        self.assertEqual(result["activity"]["scope"], "recent")
        self.assertTrue(result["activity"]["log_truncated"])
        self.assertEqual(result["activity"]["commands_completed"], 1)

    def test_detached_finalizer_publishes_without_any_caller_poll(self):
        """Real supervisor process, but the only worker is a synthetic file writer."""
        self.launcher.stop()
        original_create = self.create_worker

        def completed_worker(host, workspace, prompt, *, title, mode, options):
            worker = original_create(host, workspace, prompt, title=title, mode=mode, options=options)
            path = Path(worker["path"]) / "task.json"
            worker.update(status="completed", returncode=0)
            write_json(path, worker)
            # The native model would author files here; no model is launched.
            self.candidate({"components_path": str(Path(workspace) / "components"),
                            "report_path": str(Path(workspace) / "REPORT.md")})
            return worker

        package_root = str(Path(workflow.__file__).resolve().parents[2])
        pythonpath = package_root + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
        popen = workflow.subprocess.Popen
        children = []

        def spawn(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            self.addCleanup(lambda: child.wait(timeout=5))
            return child

        with patch.object(self.tasks, "start", side_effect=completed_worker), patch.dict(os.environ, {"PYTHONPATH": pythonpath}), \
                patch.object(workflow.subprocess, "Popen", side_effect=spawn):
            job = self.begin()
            filename = Path(job["output_path"]) / "job.json"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                saved = read_json(filename)
                if saved.get("finalized_at"):
                    break
                time.sleep(0.05)
        self.assertTrue(saved.get("finalized_at"), "Detached supervisor did not finalize the synthetic completed worker")
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["valid_candidate_count"], 1)
        self.assertTrue(Path(saved["candidates"][0]["archive_path"]).is_file())
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main()
