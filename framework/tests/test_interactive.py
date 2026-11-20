import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from skillrl.interactive import ActionValidationError, TaskBroker, TaskServer, native_arguments, run_native, summarize_native
from skillrl.interactive import native_task_run_args
from skillrl.mcp_stdio import stop_decision


class CounterTask:
    def __init__(self):
        self.count = 0
        self.done = False

    def status(self):
        return {"done": self.done, "cwd": "/testbed", "query": "Increment once", "response_schema": {"type": "object"}}

    def act(self, action):
        if action != {"increment": 1}:
            raise ActionValidationError("Expected increment=1")
        self.count += 1
        self.done = True
        return {"done": True, "observation": "incremented"}


class InteractiveTests(unittest.TestCase):
    def test_sqlite_actions_run_on_the_original_task_thread(self):
        class SQLiteTask:
            def __init__(self):
                self.connection = sqlite3.connect(":memory:")

            def status(self):
                return {"value": self.connection.execute("SELECT 1").fetchone()[0]}

            def act(self, action):
                return self.status()

        with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=1) as owner:
            task = owner.submit(SQLiteTask).result()
            broker = TaskBroker(task, Path(tmp), owner=owner)
            result = broker.call("sqlite1", "act", {"action": {}})
            self.assertFalse(result["isError"])
            self.assertEqual(json.loads(result["content"][0]["text"])["value"], 1)
            owner.submit(task.connection.close).result()

    def test_native_stop_requires_official_completion_or_interruption(self):
        unfinished = {"isError": False, "content": [{"type": "text", "text": '{"done":false}'}]}
        self.assertEqual(stop_decision(unfinished)["decision"], "block")
        finished = {"isError": False, "content": [{"type": "text", "text": '{"done":true}'}]}
        self.assertEqual(stop_decision(finished), {})
        self.assertEqual(stop_decision({"isError": True}), {})

    def test_offline_task_gets_control_network_and_survives_stop_for_archiving(self):
        original = ["--rm", "--network=none", "--memory=4g"]
        self.assertEqual(native_task_run_args(original, "internal-control"), ["--network=internal-control", "--memory=4g"])
        self.assertEqual(original, ["--rm", "--network=none", "--memory=4g"])

    def test_native_arguments_keep_default_prompt_and_task_cwd(self):
        args = native_arguments("container", "/testbed", "/tmp/run", "http://bridge/token", "claude-opus-5-5", 64)
        self.assertNotIn("--bare", args)
        self.assertNotIn("--system-prompt", args)
        self.assertEqual(args[args.index("-w") + 1], "/testbed")
        self.assertIn("--mcp-config", args)
        self.assertIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY=1", args)

    def test_same_operation_is_applied_once_and_conflicting_reuse_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = CounterTask()
            broker = TaskBroker(task, Path(tmp))
            first = broker.call("op1", "act", {"action": {"increment": 1}})
            self.assertEqual(first, broker.call("op1", "act", {"action": {"increment": 1}}))
            self.assertEqual(task.count, 1)
            with self.assertRaises(ValueError):
                broker.call("op1", "status", {})

    def test_pending_operation_blocks_reexecution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "op1").mkdir()
            (root / "op1/intent.json").write_text(json.dumps({"tool": "act", "arguments": {"action": {"increment": 1}}}))
            task = CounterTask()
            with self.assertRaises(RuntimeError):
                TaskBroker(task, root).call("op1", "act", {"action": {"increment": 1}})
            self.assertEqual(task.count, 0)

    def test_validation_failure_is_returned_without_inventing_task_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = CounterTask()
            result = TaskBroker(task, Path(tmp)).call("op1", "act", {"action": {"increment": 2}})
            self.assertTrue(result["isError"])
            self.assertFalse(task.done)
            self.assertEqual(task.count, 0)

    def test_native_partial_tool_is_explicitly_incomplete(self):
        events = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "call1", "name": "mcp__benchmark__act", "input": {}}]}}]
        summary = summarize_native(events)
        self.assertEqual(summary["missing_tool_returns"], ["call1"])
        self.assertFalse(summary["complete"])

    def test_tools_close_only_with_actual_return_and_terminal(self):
        events = [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "call1", "name": "mcp__benchmark__act", "input": {}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call1", "content": "done"}]}},
            {"type": "result", "is_error": False, "result": "finished"},
        ]
        self.assertTrue(summarize_native(events)["complete"])

    def test_timeout_preserves_partial_native_output_and_missing_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "pending", "name": "act", "input": {}}]}}
            code = "import time;print(" + repr(json.dumps(event)) + ",flush=True);time.sleep(60)"
            result = run_native([sys.executable, "-c", code], "task", Path(tmp), .2, lambda: None)
            self.assertTrue(result["timeout"])
            self.assertFalse(result["complete"])
            self.assertEqual(result["missing_tool_returns"], ["pending"])
            self.assertTrue((Path(tmp) / "native-cli.jsonl").read_bytes())

    def test_native_mcp_stdio_round_trip_to_official_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = CounterTask()
            server = TaskServer(TaskBroker(task, Path(tmp)), "127.0.0.1")
            port = server.start()
            env = dict(os.environ, BENCHMARK_RPC_URL=f"http://127.0.0.1:{port}/{server.token}")
            script = Path(__file__).resolve().parents[1] / "skillrl/mcp_stdio.py"
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "act", "arguments": {"action": {"increment": 1}}}},
            ]
            try:
                result = subprocess.run([sys.executable, str(script)], input="\n".join(json.dumps(row) for row in requests) + "\n", text=True, capture_output=True, env=env, timeout=5)
            finally:
                server.close()
            self.assertEqual(result.returncode, 0)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(replies[1]["result"]["tools"]), 2)
            self.assertFalse(replies[2]["result"]["isError"])
            self.assertEqual(task.count, 1)


if __name__ == "__main__":
    unittest.main()
