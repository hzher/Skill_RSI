"""Codex Desktop's native rollout remains usable by capture and model analysis."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skill_rsi.adapters.native_events import native_parts, native_session
from skill_rsi.adapters.session import read_session
from skill_rsi.discovery.llm_analysis import _evidence, _project_record
from skill_rsi.discovery.task_analysis import _capture, analyze_task
from skill_rsi.evidence.archive import Archive


def desktop(item):
    return {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": "desktop-thread",
                                               "turn_id": "turn-1", "item": item}}


class DesktopRolloutTests(unittest.TestCase):
    def test_pascal_case_messages_and_command_keep_native_pointers(self):
        command = desktop({"type": "CommandExecution", "id": "command-1", "command": "example",
                           "aggregated_output": "ERROR example", "exit_code": 1, "status": "Completed"})
        parts = list(native_parts(command, "codex"))
        self.assertEqual([part[0] for part in parts], ["tool_call", "tool_result"])
        self.assertEqual(parts[1][1], "/payload/item/aggregated_output")
        self.assertEqual(parts[1][2]["tool_call_id"], "command-1")
        self.assertEqual(native_session(command, "codex"), "desktop-thread")
        for kind, block, event in (("UserMessage", "text", "user_message"),
                                    ("AgentMessage", "Text", "model_response")):
            message = desktop({"type": kind, "id": "message-1", "content": [{"type": block, "text": "example"}]})
            part, = native_parts(message, "codex")
            self.assertEqual((part[0], part[1]), (event, "/payload/item/content/0/text"))

    def test_missing_call_identity_is_preserved_as_unresolved(self):
        row = {"type": "response_item", "payload": {"type": "function_call_output", "id": "output-id",
                                                     "name": "shell", "output": "example"}}
        part, = native_parts(row, "codex")
        self.assertIsNone(part[2]["tool_call_id"])
        self.assertEqual(part[2]["mapping_status"], "unresolved")

    def test_usage_compaction_and_agent_message_are_recognized(self):
        usage = {"type": "token_usage_record", "payload": {"thread_id": "desktop-thread", "usage": {
            "input_tokens": 100, "output_tokens": 30, "cached_input_tokens": 50}}}
        part, = native_parts(usage, "codex")
        self.assertEqual((part[0], part[1], part[2]["usage_scope"]), ("usage", "/payload/usage", "response"))
        compact, = native_parts(desktop({"type": "ContextCompaction", "id": "compact-1"}), "codex")
        self.assertEqual((compact[0], compact[2]["change_kind"]), ("context_change", "context_compact"))
        message = {"type": "response_item", "payload": {"type": "agent_message", "author": "agent",
                    "content": [{"type": "input_text", "text": "Public status"},
                                {"type": "encrypted_content", "encrypted_content": "PRIVATE_CIPHER"}]}}
        part, = native_parts(message, "codex")
        self.assertEqual(part[0], "model_response")
        self.assertNotIn("PRIVATE_CIPHER", json.dumps(_project_record(message)))

    def test_analysis_preserves_tool_output_and_excludes_reasoning(self):
        row = desktop({"type": "CommandExecution", "id": "command-1", "command": "example",
                       "aggregated_output": "PUBLIC_TOOL_OUTPUT"})
        projected = _project_record({"type": "tool_result", "native_record": row})
        self.assertIn("PUBLIC_TOOL_OUTPUT", json.dumps(projected))
        private = desktop({"type": "Reasoning", "id": "reason-1", "raw_content": "PRIVATE_REASONING",
                           "summary_text": "PRIVATE_REASON_SUMMARY", "aggregated_output": "PRIVATE_ALIAS"})
        part, = native_parts(private, "codex")
        self.assertEqual(part[0], "native_private_block")
        self.assertFalse(part[2]["teacher_visible"])
        self.assertNotIn("PRIVATE_", json.dumps(_project_record(private)))

    def test_saved_rollout_flows_through_reader_and_analysis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "capture").mkdir()
            rows = [
                {"type": "session_meta", "payload": {"id": "desktop-thread", "cwd": str(root),
                                                       "source": "vscode", "originator": "Codex Desktop"}},
                desktop({"type": "UserMessage", "id": "user-1", "content": [{"type": "text", "text": "Inspect failure"}]}),
                desktop({"type": "CommandExecution", "id": "command-1", "command": "example",
                         "aggregated_output": "step OK\n" * 65 + "ERROR AssertionError: public failure"}),
                desktop({"type": "Reasoning", "id": "private-1", "raw_content": "PRIVATE_REASONING"}),
                desktop({"type": "ContextCompaction", "id": "compact-1"}),
                {"type": "response_item", "payload": {"type": "function_call_output", "id": "orphan-output",
                                                         "output": "PUBLIC_ORPHAN_OUTPUT"}},
            ]
            source = root / "capture/codex-rollout.jsonl"
            source.write_text("".join(json.dumps(row) + "\n" for row in rows))
            task = {"schema": "skill-rsi.task.v1", "task_id": "task-example", "project": str(root),
                    "host": "codex", "status": "observing", "mode": "observed"}
            (root / "task.json").write_text(json.dumps(task))
            run, = read_session(Archive(root), source, "codex")
            results = [row for row in run.events if row["type"] == "tool_result"]
            self.assertEqual([row["mapping_status"] for row in results], ["native_call_id", "unresolved"])
            prompt, index = _evidence(root, task)
            self.assertIn("ERROR AssertionError", prompt)
            self.assertIn("PUBLIC_ORPHAN_OUTPUT", prompt)
            self.assertNotIn("PRIVATE_REASONING", prompt)
            self.assertEqual(index["excerpt_count"], len(rows))
            analysis = analyze_task(root)
            self.assertTrue({"continuity", "assertions", "logs"} <= {row["signal"] for row in analysis["findings"]})

    def test_existing_cli_command_events_keep_behavior(self):
        row = {"type": "item.completed", "item": {"type": "command_execution", "id": "cli-call",
                                                    "aggregated_output": "CLI_OUTPUT"}}
        part, = native_parts(row, "codex")
        self.assertEqual((part[0], part[1], part[2]["tool_call_id"]), ("tool_result", "/item", "cli-call"))
        self.assertIn("CLI_OUTPUT", json.dumps(_project_record(row)))

    def test_root_and_background_streams_share_the_analysis_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            child = root / "capture/threads/background-thread"
            child.mkdir(parents=True)
            row = desktop({"type": "UserMessage", "id": "user-1", "content": [{"type": "text", "text": "ROOT_REQUEST"}]})
            (root / "capture/codex-rollout.jsonl").write_text((json.dumps(row) + "\n") * 6)
            tools = [desktop({"type": "CommandExecution", "id": "child-command", "aggregated_output": "CHILD_OUTPUT"}),
                     desktop({"type": "Reasoning", "id": "child-private", "raw_content": "PRIVATE_CHILD_REASONING"})]
            (child / "codex-rollout.jsonl").write_text("".join(json.dumps(row) + "\n" for row in tools))
            task = {"schema": "skill-rsi.task.v1", "task_id": "root-task", "project": str(root),
                    "host": "codex", "status": "observing", "mode": "observed"}
            (root / "task.json").write_text(json.dumps(task))
            with patch("skill_rsi.discovery.task_analysis._MAX_RECORDS", 4):
                gaps = set()
                rows = _capture(root, gaps)
                self.assertEqual(len(rows), 4)
                self.assertEqual({ref["path"] for _, ref in rows}, {
                    "capture/codex-rollout.jsonl", "capture/threads/background-thread/codex-rollout.jsonl"})
                self.assertIn("capture_analysis_limit", gaps)
                prompt, index = _evidence(root, task)
            self.assertIn("ROOT_REQUEST", prompt)
            self.assertIn("CHILD_OUTPUT", prompt)
            self.assertIn("capture/threads/background-thread/codex-rollout.jsonl", prompt)
            self.assertNotIn("PRIVATE_CHILD_REASONING", prompt)
            self.assertEqual(index["excerpt_count"], 4)


if __name__ == "__main__":
    unittest.main()
