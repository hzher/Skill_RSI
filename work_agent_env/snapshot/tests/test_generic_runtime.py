"""Dataset-neutral registration, model routing, concurrency and Docker ownership checks."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from integrations.benchmark_tasks import digest, load_task_set, make_task_set
from integrations.benchmark_tasks.store import TaskStore
from integrations.executors import ExecutorOptions
from integrations.executors.api import APIExecutor
from integrations.executors.models import APIModel
from integrations.executors.tools import ToolDispatcher
from integrations.environments.docker import DockerEnvironment
from scripts import benchmark_tasks as cli

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "integrations/benchmarks/file_report"


def install(root, name="file_report"):
    target = root / "integrations/benchmarks" / name
    shutil.copytree(SAMPLE, target)
    manifest = json.loads((target / "manifest.json").read_text())
    manifest["benchmark"] = name
    (target / "manifest.json").write_text(json.dumps(manifest))
    return target


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = TaskStore(self.root)
        install(self.root)

    def test_install_another_dataset_without_any_python_registration(self):
        install(self.root, "another_dataset")
        self.assertEqual(self.store.discover(), ["another_dataset", "file_report"])
        for name in self.store.discover():
            registration = self.store.register(name)
            tasks = load_task_set(registration["task_set"])
            self.assertEqual(tasks["tasks"][0]["benchmark"], name)
            self.assertEqual(self.store.package_for(tasks["tasks"][0]), Path(registration["package"]))

    def test_snapshot_is_stable_after_source_changes_and_rejects_tampering(self):
        registration = self.store.register("file_report")
        tasks = load_task_set(registration["task_set"])
        source = self.root / "integrations/benchmarks/file_report/tasks/greeting/docs/task.md"
        source.write_text("Updated task")
        self.assertEqual(self.store.package_for(tasks["tasks"][0]), Path(registration["package"]))
        updated = self.store.register("file_report")
        self.assertNotEqual(updated["tasks_sha256"], registration["tasks_sha256"])
        snapshot = Path(registration["package"]) / "tasks/greeting/docs/task.md"
        snapshot.write_text("Corrupted")
        with self.assertRaises(ValueError): self.store.package_for(tasks["tasks"][0])

    def test_rejects_malformed_runtime_before_registration(self):
        manifest = self.root / "integrations/benchmarks/file_report/manifest.json"
        data = json.loads(manifest.read_text())
        data["evaluation"]["config"]["argv"] = "python eval.py"
        manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError): self.store.register("file_report")
        self.assertFalse((self.root / ".runtime").exists())

    def test_symlink_and_escape_are_rejected(self):
        (self.root / "integrations/benchmarks/file_report/tools/escape").symlink_to("/etc/passwd")
        with self.assertRaises(ValueError): self.store.register("file_report")

    def test_repeated_registration_keeps_same_identity(self):
        self.assertEqual(self.store.register("file_report"), self.store.register("file_report"))

    def test_cli_register_test_plan_do_not_start_models_or_containers(self):
        with patch.object(cli, "ROOT", self.root):
            self.assertEqual(cli.main(["list"]), 0)
            self.assertEqual(cli.main(["register", "--benchmark", "file_report"]), 0)
            self.assertEqual(cli.main(["test", "--benchmark", "file_report"]), 0)
            self.assertEqual(cli.main(["run", "--benchmark", "file_report", "--provider", "openai", "--model", "fixed-model"]), 0)
        self.assertFalse((self.root / ".runtime/executors").exists())

    def test_core_modules_contain_no_dataset_specific_dependencies(self):
        # Check all registered dataset names, rather than hard-coding one historical name.
        names = ["file_report", "another_dataset", "toolathlon", "toolathon"]
        for directory in ("benchmark_tasks", "executors", "environments", "verifiers"):
            for path in (ROOT / "integrations" / directory).glob("*.py"):
                text = path.read_text().lower()
                for name in names:
                    self.assertNotIn(name, text, str(path))


class ModelClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_tool_catalog_is_searchable_without_exposing_all_definitions(self):
        called = []
        async def invoke(name, arguments): called.append((name, arguments)); return {"ok": True}
        tools = [{"name": "service__tool" + str(index), "description": "Example", "parameters": {"type": "object"}} for index in range(150)]
        dispatcher = ToolDispatcher(tools, invoke)
        self.assertEqual(len(dispatcher.definitions()), 2)
        result = await dispatcher.call("search_available_tools", {"query": "tool149"})
        self.assertEqual(result["tools"][0]["name"], "service__tool149")
        await dispatcher.call("call_available_tool", {"name": "service__tool149", "arguments": {"x": 1}})
        self.assertEqual(called, [("service__tool149", {"x": 1})])
        with self.assertRaises(ValueError):
            await dispatcher.call("call_available_tool", {"name": "invented", "arguments": {}})

    async def test_fixed_openai_model_and_tool_result_messages(self):
        import sys
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())), close=AsyncMock())
        client.chat.completions.create.return_value = SimpleNamespace(model="fixed-id", usage=None,
            choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[
                SimpleNamespace(id="call1", function=SimpleNamespace(name="write_file", arguments='{"path":"result.txt","content":"hello"}'))]))])
        with patch.dict("os.environ", {"MODEL_API_KEY": "test-only"}), patch.dict(sys.modules,
                {"openai": SimpleNamespace(AsyncOpenAI=lambda **kwargs: client)}):
            model = APIModel(ExecutorOptions("openai", model="fixed-id"))
            reply = await model.turn("system", [{"name": "write_file", "description": "Write", "parameters": {"type": "object"}}],
                [{"role": "user", "content": "task"}])
            self.assertEqual(reply["calls"][0]["name"], "write_file")
            await model.turn("system", [], [{"role": "user", "content": "task"}, {"role": "assistant", **reply},
                {"role": "tool", "id": "call1", "content": "written"}])
            request = client.chat.completions.create.call_args.kwargs
            self.assertEqual(request["model"], "fixed-id")
            # Dynamic MCP argument schemas are intentionally best-effort. Be
            # explicit: some compatible gateways infer strict mode if omitted.
            await model.turn("system", [{"name":"echo","description":"Echo","parameters":{"type":"object"}}],
                [{"role":"user","content":"test"}])
            self.assertIs(client.chat.completions.create.call_args.kwargs["tools"][0]["function"]["strict"],False)
            self.assertEqual(request["messages"][-1]["tool_call_id"], "call1")
            await model.close()

    async def test_fixed_anthropic_model_combines_tool_results(self):
        import sys
        client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()), close=AsyncMock())
        client.messages.create.return_value = SimpleNamespace(model="fixed-id", stop_reason="tool_use",
            usage=SimpleNamespace(model_dump=lambda: {}), content=[SimpleNamespace(type="tool_use", id="call1", name="write_file", input={"path": "x", "content": "y"})])
        with patch.dict("os.environ", {"MODEL_API_KEY": "test-only"}), patch.dict(sys.modules,
                {"anthropic": SimpleNamespace(AsyncAnthropic=lambda **kwargs: client)}):
            model = APIModel(ExecutorOptions("anthropic", model="fixed-id"))
            reply = await model.turn("system", [], [{"role": "user", "content": "task"}])
            await model.turn("system", [], [{"role": "user", "content": "task"}, {"role": "assistant", **reply},
                {"role": "tool", "id": "call1", "content": "first"}, {"role": "tool", "id": "call2", "content": "second"}])
            request = client.messages.create.call_args.kwargs
            self.assertEqual(request["model"], "fixed-id")
            self.assertEqual(len(request["messages"][-1]["content"]), 2)
            await model.close()


class Driver:
    def __init__(self, options): pass
    async def turn(self, system, tools, history):
        if any(row["role"] == "tool" for row in history):
            return {"text": "Done", "calls": [], "usage": {}, "model": "test-driver"}
        return {"text": "", "calls": [{"id": "call-1", "name": "write_file",
            "arguments": {"path": "result.txt", "content": "hello\n"}}], "usage": {}, "model": "test-driver"}
    async def close(self): pass


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        install(self.root)
        store = TaskStore(self.root)
        self.tasks = load_task_set(store.register("file_report")["task_set"])

    async def test_model_tool_result_loop_and_bounded_parallelism(self):
        active = 0
        peak = 0
        class Environment:
            def __init__(self, **kwargs): self.value = None
            async def initialize(self):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(.02)
                return {"image_id": "test-image"}
            def discover(self): return [{"name": "write_file", "description": "Write", "parameters": {"type": "object"}}]
            async def call(self, name, arguments): self.value = arguments["content"]; return {"ok": True}
            async def evaluate(self): return {"pass": self.value == "hello\n"}
            async def close(self):
                nonlocal active
                active -= 1
                return {"resources_closed": True}
        # Different registered datasets can be blended without a new executor.
        install(self.root, "another_dataset")
        store = TaskStore(self.root)
        other = load_task_set(store.register("another_dataset")["task_set"])
        tasks = make_task_set(self.tasks["tasks"] + other["tasks"])
        executor = APIExecutor(project_root=self.root, model_factory=Driver, environment_factory=Environment)
        receipt = await executor.execute_async(tasks, ExecutorOptions("openai", model="test-driver", concurrency=2))
        self.assertEqual(peak, 2)
        self.assertTrue(receipt["resources_closed"])
        self.assertEqual([row["evaluation"]["reward"] for row in receipt["results"]], [1, 1])
        self.assertTrue(all(row["status"] == "completed" for row in receipt["results"]))
        with self.assertRaises(FileExistsError):
            await executor.execute_async(tasks, ExecutorOptions("openai", model="test-driver", run_id=receipt["run_id"]))

    async def test_model_timeout_cleans_environment_and_keeps_reward_unknown(self):
        closed = []
        class Environment:
            def __init__(self, **kwargs): pass
            async def initialize(self): return {"image_id": "test-image"}
            def discover(self): return [{"name": "write_file"}]
            async def close(self): closed.append(True); return {"resources_closed": True}
        class Slow(Driver):
            async def turn(self, *args): await asyncio.sleep(10)
        executor = APIExecutor(project_root=self.root, model_factory=Slow, environment_factory=Environment)
        result = await executor.execute_async(self.tasks, ExecutorOptions("openai", model="fixture", task_timeout=.03))
        self.assertEqual(closed, [True])
        self.assertIsNone(result["results"][0]["evaluation"])
        self.assertEqual(result["status"], "completed_with_errors")

    async def test_turn_budget_scores_partial_work_and_preserves_stop_reason(self):
        class Environment:
            def __init__(self, **kwargs): self.written=False
            async def initialize(self): return {"image_id":"test-image"}
            def discover(self):return [{"name":"write_file","description":"Write","parameters":{"type":"object"}}]
            async def call(self,name,arguments):self.written=True;return {"ok":True}
            async def evaluate(self):return {"pass":self.written}
            async def close(self):return {"resources_closed":True}
        receipt=await APIExecutor(project_root=self.root,model_factory=Driver,
            environment_factory=Environment).execute_async(self.tasks,
            ExecutorOptions("openai",model="fixture",max_turns=1))
        row=receipt["results"][0]
        self.assertEqual(row["model_stop_reason"],"turn_budget_exhausted")
        self.assertEqual(row["evaluation"]["reward"],1)
        self.assertTrue(row["resources_closed"])
        self.assertTrue((Path(receipt["receipt_path"]).parent/"source_snapshot/integrations/executors/api.py").is_file())

    async def test_real_docker_task_isolation_tools_evaluator_and_cleanup(self):
        # Explicitly run by the container acceptance suite, never part of default unit runs.
        import os
        if os.environ.get("RUN_CONTAINER_CHECKS") != "1":
            self.skipTest("Set RUN_CONTAINER_CHECKS=1 to run the owned Docker check")
        install(self.root, "another_dataset")
        other = load_task_set(TaskStore(self.root).register("another_dataset")["task_set"])
        blended = make_task_set(self.tasks["tasks"] + other["tasks"])
        class Inspected(DockerEnvironment):
            async def initialize(owned):
                result = await super().initialize()
                code, _, _ = await owned.command("exec", owned.container, "test", "-e", "/assets/evaluation/main.py", check=False)
                if code != 1:
                    raise AssertionError("Evaluator was exposed in the model container")
                return result
        executor = APIExecutor(project_root=self.root, model_factory=Driver, environment_factory=Inspected)
        result = await executor.execute_async(blended, ExecutorOptions("openai", model="test-driver", concurrency=2, task_timeout=120))
        row = result["results"][0]
        self.assertEqual(row["status"], "completed", row)
        self.assertEqual(row["evaluation"]["reward"], 1)
        self.assertTrue(row["resources_closed"], row)
        trajectory = json.loads(Path(row["trajectory"]).read_text())
        self.assertTrue(any(event["role"] == "tool" for event in trajectory))
        second = result["results"][1]
        self.assertEqual(second["evaluation"]["reward"], 1, second)
        self.assertNotEqual(row["environment"]["container"], second["environment"]["container"])
        self.assertNotEqual(row["environment"]["network"], second["environment"]["network"])
        self.assertTrue(result["resources_closed"])

    async def test_real_mcp_discovery_and_call_in_owned_container(self):
        import os
        if os.environ.get("RUN_CONTAINER_CHECKS") != "1":
            self.skipTest("Set RUN_CONTAINER_CHECKS=1 to run the owned MCP check")
        package = install(self.root, "mcp_example")
        (package / "tools/server.py").write_text(
            "from mcp.server.fastmcp import FastMCP\nfrom pathlib import Path\n"
            "server=FastMCP('files')\n@server.tool()\n"
            "def write_file(path: str, content: str) -> str:\n"
            " target=(Path('/workspace')/path).resolve()\n"
            " if not target.is_relative_to(Path('/workspace')): raise ValueError('escape')\n"
            " target.write_text(content)\n return 'written'\n"
            "server.run(transport='stdio')\n")
        manifest = json.loads((package / "manifest.json").read_text())
        manifest["environment"]["config"]["tools"] = []
        manifest["environment"]["config"]["mcp_servers"] = [{"name": "files", "argv": ["/opt/venv/bin/python3", "/assets/tools/server.py"]}]
        (package / "manifest.json").write_text(json.dumps(manifest))
        (package / "tasks/greeting/task_config.json").write_text(json.dumps({"needed_mcp_servers": ["files"], "needed_local_tools": []}))
        tasks = load_task_set(TaskStore(self.root).register("mcp_example")["task_set"])
        class MCPDriver(Driver):
            async def turn(self, system, tools, history):
                reply = await super().turn(system, tools, history)
                for call in reply["calls"]:
                    call["name"] = "files__write_file"
                return reply
        result = await APIExecutor(project_root=self.root, model_factory=MCPDriver).execute_async(
            tasks, ExecutorOptions("openai", model="test-driver", task_timeout=120))
        row = result["results"][0]
        self.assertEqual(row["status"], "completed", row)
        self.assertTrue(row["resources_closed"], row)
        self.assertEqual(row["evaluation"]["reward"], 1)

    async def test_real_private_resource_service_readiness_network_and_cleanup(self):
        import os
        if os.environ.get("RUN_CONTAINER_CHECKS") != "1":
            self.skipTest("Set RUN_CONTAINER_CHECKS=1 to run the owned service check")
        package = install(self.root, "resource_example")
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        config = manifest["environment"]["config"]
        config["services"] = [{"name": "database", "image": "postgres:15",
            "env": {"POSTGRES_USER": "tester", "POSTGRES_DB": "testdb", "POSTGRES_PASSWORD": "owned-test-only"},
            "ready": ["pg_isready", "-h", "127.0.0.1", "-U", "tester", "-d", "testdb"]}]
        config["tools"].append({"name": "check_service", "description": "Check the task's private resource service",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            "argv": ["python3", "-c", "import socket; socket.create_connection(('database',5432),timeout=5).close(); print('connected')"]})
        manifest_path.write_text(json.dumps(manifest))
        tasks = load_task_set(TaskStore(self.root).register("resource_example")["task_set"])
        class ServiceDriver(Driver):
            async def turn(self, system, tools, history):
                reply = await super().turn(system, tools, history)
                if reply["calls"]:
                    reply["calls"].insert(0, {"id": "connection", "name": "check_service", "arguments": {}})
                return reply
        result = await APIExecutor(project_root=self.root, model_factory=ServiceDriver).execute_async(
            tasks, ExecutorOptions("openai", model="test-driver", task_timeout=120))
        row = result["results"][0]
        self.assertEqual(row["status"], "completed", row)
        self.assertTrue(row["resources_closed"], row)
        self.assertIn("database", row["environment"]["service_images"])
        trajectory = json.loads(Path(row["trajectory"]).read_text())
        connection = next(event for event in trajectory if event["role"] == "tool" and event["id"] == "connection")
        self.assertEqual(json.loads(connection["content"])["exit_code"], 0)
