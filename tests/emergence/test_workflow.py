"""Independent checks for complete local evidence and nonexecuting artifact review."""

import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from skill_rsi.emergence import poll, start
from skill_rsi.components.archive import read_component
from skill_rsi.tasks.store import TaskStore


class EmergenceWorkflowTests(unittest.TestCase):
    def setUp(self):
        finalizer = patch("skill_rsi.emergence.workflow._launch_finalizer", return_value=False, create=True)
        finalizer.start()
        self.addCleanup(finalizer.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.task = self.root / "source-task"
        self.capture = self.task / "capture"
        self.capture.mkdir(parents=True)
        self.state = self.root / "state"
        self.state.mkdir()
        project = self.root / "project"
        project.mkdir()
        (self.task / "task.json").write_text(json.dumps({
            "schema": "skill-rsi.task.v1", "task_id": "source-task", "host": "codex", "mode": "observed",
            "status": "observing", "source": "desktop", "project": str(project), "path": str(self.task),
            "capture_path": str(self.capture), "capture_policy": "new_only",
            "coverage_gaps": ["history_before_recording_not_captured"],
        }))
        job_path = self.root / "mock-model-job"
        job_capture = job_path / "capture"
        job_capture.mkdir(parents=True)
        self.model_job = {"task_id": "model-job-test", "status": "running", "path": str(job_path),
                          "capture_path": str(job_capture)}
        self.tasks = Mock(spec=TaskStore)
        self.tasks.start.return_value = dict(self.model_job)
        self.tasks.get.side_effect = lambda _id: dict(self.model_job)

    @staticmethod
    def line(value):
        return (json.dumps(value, ensure_ascii=False) + "\n").encode()

    @staticmethod
    def message(text):
        return {"timestamp": "2026-11-25T06:00:00Z", "type": "event_msg",
                "payload": {"type": "item_completed", "item": {
                    "type": "AgentMessage", "content": [{"type": "Text", "text": text}]}}}

    def write_stream(self, relative="capture/codex-rollout.jsonl", records=None):
        path = self.task / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(self.line(row) for row in (records or [self.message("visible task fact")])))
        return path

    def begin(self, **options):
        return start(self.task, self.tasks, self.state, **options)

    def finish(self):
        self.model_job.update(status="completed", returncode=0)
        return poll(self.task, self.tasks)

    def candidate(self, job, component="free-skill", *, engine="skill", form="skill", payload=None):
        root = Path(job["workspace_path"]) / "components" / component
        root.mkdir(parents=True)
        entrypoint = "payload/SKILL.md" if engine == "skill" else "payload/policy.py"
        target = root / entrypoint
        target.parent.mkdir()
        target.write_text(payload or "---\nname: free-skill\ndescription: A source-backed candidate\n---\n\n"
                          "# 保留当前结论\n\n只在需要接续任务时核对当前来源和已有结论。\n")
        manifest = {"schema": "skill-rsi.component.v1", "component_id": component, "version": "1.0.0",
                    "form": form, "engine": engine, "entrypoint": entrypoint,
                    "scope": {"hosts": ["codex"], "modes": ["shadow", "suggest", "apply"]},
                    "trigger": {"boundaries": ["before_action"]},
                    "action": {"kind": "append_context", "target": "context." + component, "writes": []},
                    "required_capabilities": [], "budget": {"max_action_bytes": 8192}, "fallback": "native"}
        (root / "manifest.json").write_text(json.dumps(manifest))
        (root / "README.md").write_text("# 候选扩展\n\n用于接续长任务，效果尚未验证，先人工阅读再决定是否安装。\n")
        (root / "review.json").write_text(json.dumps({
            "display_name": "接续任务候选", "description": "从轨迹证据发现、等待独立验证的扩展。",
            "category": "模型自由命名的记忆机制", "use_when": ["接续长任务，需要沿用已有结论。"],
            "skip_when": ["当前已有充分证据。"], "mechanism": "保留依据并核对当前来源。",
            "limitations": ["仅静态检查，尚未运行或评估效果。"],
            "evaluation_plan": ["在独立任务中比较启用和停用时的正确率与重复读取。"],
        }, ensure_ascii=False))
        return root

    def test_complete_root_child_evidence_is_not_sampled_or_text_truncated(self):
        records = [self.message(f"ROOT_EVENT_{number:04d}") for number in range(2105)]
        long_output = "LONG_OUTPUT_BEGIN_" + "详细输出" * 4000 + "_LONG_OUTPUT_END"
        records.append({"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "CommandExecution", "id": "long-command", "aggregated_output": long_output, "exit_code": 0}}})
        self.write_stream(records=records)
        self.write_stream("capture/threads/worker/codex-rollout.jsonl",
                          [self.message(f"CHILD_EVENT_{number:04d}") for number in range(75)])
        job = self.begin()
        snapshot = Path(job["snapshot_path"])
        text = "\n".join(path.read_text() for path in snapshot.rglob("*.jsonl"))
        for number in range(2105):
            self.assertIn(f"ROOT_EVENT_{number:04d}", text)
        for number in range(75):
            self.assertIn(f"CHILD_EVENT_{number:04d}", text)
        self.assertIn(long_output, text)
        self.assertGreaterEqual(job["snapshot"]["record_count"], 2181)
        self.assertEqual(job["snapshot"]["stream_count"], 2)
        self.assertEqual(job["status"], "running")

    def test_nested_private_reasoning_and_credentials_are_excluded(self):
        records = [self.message("API_KEY=sk-syntheticcredential123456 visible public fact"),
                   {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                       "type": "Reasoning", "raw_content": "PRIVATE_DESKTOP_REASONING_SENTINEL"}}},
                   {"type": "response_item", "payload": {"type": "reasoning",
                                                               "content": "PRIVATE_RESPONSE_REASONING_SENTINEL"}},
                   {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                       {"type": "thinking", "text": "PRIVATE_NESTED_REASONING_SENTINEL"},
                       {"type": "output_text", "text": "VISIBLE_ASSISTANT_FACT"}]}},
                   {"type": "custom_event", "payload": {"api_key": "UNPREFIXED_CREDENTIAL_SENTINEL",
                                                                "password": "PASSWORD_CREDENTIAL_SENTINEL",
                                                                "authToken": "AUTH_TOKEN_CREDENTIAL_SENTINEL",
                                                                "output": "VISIBLE_CUSTOM_FIELD"}},
                   self.message("ANTHROPIC_AUTH_TOKEN=ANTHROPIC_ROUTER_CREDENTIAL_SENTINEL\n"
                                "AWS_SECRET_ACCESS_KEY=AWS_SECRET_CREDENTIAL_SENTINEL")]
        self.write_stream(records=records)
        job = self.begin()
        snapshot = Path(job["snapshot_path"])
        text = "\n".join(path.read_text() for path in snapshot.rglob("*") if path.is_file())
        for private in ("sk-syntheticcredential123456", "PRIVATE_DESKTOP_REASONING_SENTINEL",
                        "PRIVATE_RESPONSE_REASONING_SENTINEL", "PRIVATE_NESTED_REASONING_SENTINEL",
                        "UNPREFIXED_CREDENTIAL_SENTINEL", "PASSWORD_CREDENTIAL_SENTINEL",
                        "AUTH_TOKEN_CREDENTIAL_SENTINEL", "ANTHROPIC_ROUTER_CREDENTIAL_SENTINEL",
                        "AWS_SECRET_CREDENTIAL_SENTINEL"):
            self.assertNotIn(private, text)
        for visible in ("visible public fact", "VISIBLE_ASSISTANT_FACT", "VISIBLE_CUSTOM_FIELD"):
            self.assertIn(visible, text)

    def test_source_and_sanitized_index_hashes_and_offsets_can_be_verified(self):
        records = [self.message("FIRST"), self.message("API_KEY=sk-synthetichashcheck123 SECOND"),
                   {"type": "response_item", "payload": {"type": "reasoning", "content": "PRIVATE"}}]
        source = self.write_stream(records=records)
        job = self.begin()
        snapshot = Path(job["snapshot_path"])
        index = [json.loads(line) for line in (snapshot / "source-index.jsonl").read_text().splitlines()]
        self.assertEqual(len(index), len(records))
        raw_lines = source.read_bytes().splitlines(keepends=True)
        for entry in index:
            origin = entry["source"]
            self.assertEqual(origin["path"], "capture/codex-rollout.jsonl")
            position = origin["line"] - 1
            raw = raw_lines[position]
            self.assertEqual(origin["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(origin["bytes"], len(raw))
            self.assertEqual(origin["byte_offset"], sum(map(len, raw_lines[:position])))
            if entry["snapshot"] is None:
                self.assertEqual(entry["status"], "excluded_private")
                continue
            ref = entry["snapshot"]
            sanitized = (snapshot / ref["path"]).read_bytes().splitlines(keepends=True)[ref["line"] - 1]
            self.assertEqual(ref["sha256"], hashlib.sha256(sanitized).hexdigest())
            self.assertEqual(ref["bytes"], len(sanitized))

    def test_sdk_contains_manifest_templates_and_real_runtime_contracts(self):
        self.write_stream()
        job = self.begin()
        sdk = Path(job["sdk_path"])
        self.assertTrue((sdk / "SDK.md").is_file())
        examples = []
        for path in sdk.rglob("*.json"):
            try:
                value = json.loads(path.read_text())
            except ValueError:
                continue
            for example in value if isinstance(value, list) else [value]:
                if isinstance(example, dict) and example.get("schema") == "skill-rsi.component.v1":
                    examples.append(example)
        self.assertTrue(examples, "Generator needs an actual component manifest template")
        contracts = "\n".join(path.read_text() for path in sdk.rglob("*.py"))
        self.assertIn("class RuntimeEvent", contracts)
        self.assertIn("class PolicyContext", contracts)
        self.assertIn("skill-rsi.middleware-stdio.v1", "\n".join(path.read_text() for path in sdk.rglob("*") if path.is_file()))

    def test_start_uses_requested_model_effort_and_workspace_sandbox(self):
        self.write_stream()
        job = self.begin(instructions="只从证据发现候选", model="gpt-6.1-sol", effort="ultra", command="/mock/codex")
        self.tasks.start.assert_called_once()
        call = self.tasks.start.call_args
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(Path(call.args[1]), Path(job["workspace_path"]))
        options = call.kwargs["options"]
        self.assertEqual(options["model"], "gpt-6.1-sol")
        self.assertEqual(options["command"], "/mock/codex")
        args = options.get("args", [])
        self.assertIn("workspace-write", args)
        self.assertIn("--ignore-user-config", args)
        self.assertTrue(any("model_reasoning_effort" in value and "ultra" in value for value in args))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertNotIn("--trust", args)
        self.assertEqual(job["model"], "gpt-6.1-sol")
        self.assertEqual(job["effort"], "ultra")

    def test_free_skill_and_python_are_only_packed_without_execution_or_install(self):
        self.write_stream()
        job = self.begin()
        self.candidate(job)
        self.candidate(job, "unexecuted-policy", engine="python", form="plugin",
                       payload="raise AssertionError('Generated policy must never execute during review')\n")
        with patch("skill_rsi.components.registry.ComponentRegistry.install", side_effect=AssertionError("Unexpected installation")):
            result = self.finish()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["validation_status"], "static_only")
        self.assertTrue(result["review_required"])
        self.assertEqual(len(result["candidates"]), 2)
        for candidate in result["candidates"]:
            self.assertEqual(candidate["status"], "valid")
            self.assertEqual(candidate["execution_status"], "not_verified")
            self.assertTrue(candidate["human_review_required"])
            self.assertTrue(candidate["display_name"])
            self.assertTrue(candidate["use_when"])
            self.assertTrue(candidate["limitations"])
            manifest, entries = read_component(candidate["archive_path"])
            self.assertEqual(manifest["component_id"], candidate["component_id"])
            self.assertTrue(entries)

    def test_bad_candidate_does_not_hide_a_valid_neighbor(self):
        self.write_stream()
        job = self.begin()
        self.candidate(job)
        bad = self.candidate(job, "bad-path")
        value = json.loads((bad / "manifest.json").read_text())
        value["entrypoint"] = "../escape.py"
        (bad / "manifest.json").write_text(json.dumps(value))
        result = self.finish()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(sorted(row["status"] for row in result["candidates"]), ["invalid", "valid"])
        self.assertTrue(next(row for row in result["candidates"] if row["status"] == "invalid")["error"])
        valid = next(row for row in result["candidates"] if row["status"] == "valid")
        self.assertEqual(read_component(valid["archive_path"])[0]["component_id"], "free-skill")

    def test_missing_review_metadata_is_an_independent_invalid_candidate(self):
        self.write_stream()
        job = self.begin()
        self.candidate(job)
        incomplete = self.candidate(job, "missing-review")
        (incomplete / "review.json").unlink()
        result = self.finish()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(sorted(row["status"] for row in result["candidates"]), ["invalid", "valid"])

    def test_zero_candidates_is_a_successful_model_run(self):
        self.write_stream()
        job = self.begin()
        (Path(job["workspace_path"]) / "REPORT.md").write_text("现有证据不足以提出可靠策略，保留零候选。")
        result = self.finish()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["candidates"], [])
        self.assertTrue(Path(result["report_path"]).is_file())

    def test_running_or_failed_model_does_not_publish_generated_candidates(self):
        self.write_stream()
        job = self.begin()
        self.candidate(job)
        running = poll(self.task, self.tasks)
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["candidates"], [])
        self.model_job.update(status="failed", error="synthetic CLI failure", returncode=1)
        failed = poll(self.task, self.tasks)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["candidates"], [])
        self.assertFalse(list(Path(job["output_path"]).rglob("*.zip")))

    def test_symlink_candidate_is_rejected_without_reading_or_packing_external_payload(self):
        self.write_stream()
        job = self.begin()
        external = self.root / "external-candidate"
        external.mkdir()
        (external / "manifest.json").write_text("{}")
        components = Path(job["workspace_path"]) / "components"
        components.mkdir(exist_ok=True)
        (components / "linked").symlink_to(external, target_is_directory=True)
        result = self.finish()
        self.assertTrue(result["candidates"])
        self.assertTrue(all(row["status"] == "invalid" for row in result["candidates"]))
        self.assertFalse(list(Path(job["output_path"]).rglob("*.zip")))

    def test_modified_snapshot_cannot_produce_an_approved_artifact(self):
        self.write_stream()
        job = self.begin()
        self.candidate(job)
        stream = next((Path(job["snapshot_path"]) / "streams").rglob("*.jsonl"))
        self.assertFalse(stream.stat().st_mode & 0o222)
        stream.chmod(0o600)
        stream.write_bytes(stream.read_bytes() + self.line(self.message("untrusted replacement evidence")))
        result = self.finish()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["candidates"], [])
        self.assertFalse(list(Path(job["output_path"]).rglob("*.zip")))

    def test_poll_without_start_does_not_create_an_analysis(self):
        result = poll(self.task, self.tasks)
        self.assertEqual(result["status"], "not_started")
        self.assertEqual(result["candidates"], [])
        self.tasks.start.assert_not_called()
        self.tasks.get.assert_not_called()

    def test_large_report_preview_is_explicitly_truncated_and_original_is_unchanged(self):
        self.write_stream()
        job = self.begin()
        report = Path(job["report_path"])
        original = b"REPORT_BEGIN\n" + b"x" * 300000 + b"\nREPORT_END"
        report.write_bytes(original)
        running = poll(self.task, self.tasks)
        self.assertTrue(running["report_present"])
        self.assertTrue(running["report_truncated"])
        self.assertEqual(running["report"].encode(), original[:256 * 1024])
        self.assertEqual(Path(running["report_path"]), report)
        self.assertEqual(report.read_bytes(), original)
        finished = self.finish()
        self.assertTrue(finished["report_truncated"])
        self.assertEqual(report.read_bytes(), original)

    def test_running_diagnostics_show_worker_and_safe_log_tails(self):
        self.write_stream()
        job = self.begin()
        capture = Path(self.model_job["capture_path"])
        stdout = b"DISCARDED_OVERSIZE_PREFIX" * 500 + b"\n" + b"".join(self.line(row) for row in [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "VISIBLE_STDOUT api_key=sk-diagnosticssecret123"}},
            {"type": "response_item", "payload": {"type": "reasoning", "content": "PRIVATE_DIAGNOSTICS_REASONING"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "Reasoning", "raw_content": "PRIVATE_DESKTOP_DIAGNOSTICS_REASONING"}}},
        ])
        (capture / "stdout.jsonl").write_bytes(stdout)
        (capture / "stderr.log").write_text("VISIBLE_STDERR ANTHROPIC_AUTH_TOKEN=STDERR_CREDENTIAL_SENTINEL")
        (Path(self.model_job["path"]) / "worker.log").write_text("VISIBLE_WORKER password=WORKER_CREDENTIAL_SENTINEL")
        self.model_job["updated_at"] = "2026-11-25T08:00:00Z"
        running = poll(self.task, self.tasks)
        text = running["diagnostics"]
        for visible in ("running", "VISIBLE_STDOUT", "VISIBLE_STDERR", "VISIBLE_WORKER", "2026-11-25T08:00:00Z"):
            self.assertIn(visible, text)
        for private in ("sk-diagnosticssecret123", "STDERR_CREDENTIAL_SENTINEL", "WORKER_CREDENTIAL_SENTINEL",
                        "PRIVATE_DIAGNOSTICS_REASONING", "PRIVATE_DESKTOP_DIAGNOSTICS_REASONING", "DISCARDED_OVERSIZE_PREFIX"):
            self.assertNotIn(private, text)
        repeated = self.begin()
        self.assertEqual(repeated["job_id"], job["job_id"])
        self.assertIn("VISIBLE_STDOUT", repeated["diagnostics"])
        self.tasks.start.assert_called_once()

    def test_max_effort_sdk_python_and_candidate_readiness_are_explicit(self):
        self.write_stream()
        job = self.begin(effort="max")
        args = self.tasks.start.call_args.kwargs["options"]["args"]
        self.assertTrue(any("model_reasoning_effort" in arg and "max" in arg for arg in args))
        guide = (Path(job["sdk_path"]) / "SDK.md").read_text()
        self.assertIn(shlex.quote(sys.executable) + " -m skill_rsi.emergence.check", guide)
        self.assertNotIn("`python -m skill_rsi.emergence.check", guide)
        self.candidate(job, "pending-executor", engine="python", form="plugin",
                       payload="raise AssertionError('Static review must not execute this code')\n")
        result = self.finish()
        candidate, = result["candidates"]
        self.assertTrue(candidate["execution_note"])
        self.assertEqual(candidate["runtime_support"], "requires_trusted_or_isolated_executor")
        self.assertEqual(candidate["readiness"], {"packaging": "valid", "execution": "not_verified", "effectiveness": "not_evaluated"})
        self.assertTrue(candidate["human_review_required"])

    def test_large_command_record_retains_progress_without_copying_tool_output(self):
        self.write_stream()
        self.begin()
        capture = Path(self.model_job["capture_path"])
        output = "TOOL_OUTPUT_MUST_NOT_APPEAR_" * 8000
        records = [
            {"type": "item.completed", "item": {"type": "command_execution",
                "command": "echo VISIBLE_LARGE_COMMAND api_key=sk-largecommandcredential123",
                "status": "completed", "exit_code": 0, "aggregated_output": output}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "VISIBLE_PROGRESS_NOTE"}},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE_LARGE_LOG_REASONING"}},
        ]
        raw = b"".join(self.line(row) for row in records)
        self.assertGreater(len(raw), 200 * 1024)
        (capture / "stdout.jsonl").write_bytes(raw)
        result = poll(self.task, self.tasks)
        diagnostics = result["diagnostics"]
        for visible in ("VISIBLE_LARGE_COMMAND", "completed", "VISIBLE_PROGRESS_NOTE"):
            self.assertIn(visible, diagnostics)
        self.assertIn('"status":"completed"', diagnostics)
        self.assertIn('"exit_code":0', diagnostics)
        for excluded in ("TOOL_OUTPUT_MUST_NOT_APPEAR_", "sk-largecommandcredential123", "PRIVATE_LARGE_LOG_REASONING"):
            self.assertNotIn(excluded, diagnostics)
        self.assertLess(len(diagnostics.encode()), 9 * 1024)
        self.assertEqual((capture / "stdout.jsonl").read_bytes(), raw)

    def test_command_exceeding_scan_window_reports_omitted_event_explicitly(self):
        self.write_stream()
        self.begin()
        capture = Path(self.model_job["capture_path"])
        oversized = {"type": "item.completed", "item": {"type": "command_execution",
            "command": "echo OVERSIZED_COMMAND_NOT_SCANNED", "status": "completed", "exit_code": 0,
            "aggregated_output": "OVERSIZED_TOOL_OUTPUT_SENTINEL_" * 100000}}
        (capture / "stdout.jsonl").write_bytes(self.line(oversized))
        diagnostics = poll(self.task, self.tasks)["diagnostics"]
        self.assertIn("已跳过", diagnostics)
        self.assertIn("2MiB", diagnostics)
        self.assertNotIn("OVERSIZED_TOOL_OUTPUT_SENTINEL_", diagnostics)
        self.assertLess(len(diagnostics.encode()), 9 * 1024)


if __name__ == "__main__":
    unittest.main()
