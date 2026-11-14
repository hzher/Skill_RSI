"""Check native callback forwarding and timeout evidence without model calls."""
import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import subprocess
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sources/continual-learning-bench-pr26-lf"))
import clbench_bridge
from src.systems.codex.system import CodexSystem


@dataclass
class FakeObservation:
    content: str


def main():
    bridge = ROOT / "scripts/clbench_bridge.py"
    tree = ast.parse(bridge.read_text(encoding="utf-8"))
    definition = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "RouterConfiguredCodex")
    with tempfile.TemporaryDirectory(prefix="rsi-callback-check-") as temp:
        archive = Path(temp)
        (archive / "control").mkdir()
        namespace = dict(vars(clbench_bridge)) | {"CodexSystem": CodexSystem, "plan": {"include_baseline": False, "action_transport": "external_json"}, "archive": archive}
        exec(compile(ast.Module(body=[definition], type_ignores=[]), str(bridge), "exec"), namespace)
        adapter = namespace["RouterConfiguredCodex"].__new__(namespace["RouterConfiguredCodex"])
        adapter._container_id = "callback-test-container"
        adapter._timeout = 1
        observation = FakeObservation("public feedback")
        query = SimpleNamespace(instance_id="test", instance_index=0, prompt="public prompt")
        with patch.object(CodexSystem, "_build_prompt", return_value="original public prompt") as native:
            built = adapter._build_prompt(query, include_skill=False)
            assert built.endswith("original public prompt") and "external benchmark runner" in built
            native.assert_called_once_with(query, include_skill=False)
        with patch.object(CodexSystem, "observe", return_value=None) as native:
            adapter.observe(observation, query)
            native.assert_called_once_with(observation, query)
        result = SimpleNamespace(action={"command": "echo test"})
        with patch.object(CodexSystem, "respond", return_value=result) as native:
            assert adapter.respond(query) is result
            native.assert_called_once_with(query)
        with patch.object(CodexSystem, "respond", side_effect=RuntimeError("synthetic")):
            try:
                adapter.respond(query)
                raise AssertionError("Native exception was swallowed")
            except RuntimeError:
                pass
        timeout = subprocess.TimeoutExpired(["not-serialized"], 1, output=b'{"partial":true}\n', stderr=b'partial diagnostic')
        with patch.object(clbench_bridge.subprocess, "run", side_effect=timeout):
            try:
                adapter._docker_exec("codex exec", timeout=1)
                raise AssertionError("Native timeout was swallowed")
            except subprocess.TimeoutExpired:
                pass
        calls = archive / "control/native-calls"
        assert next(calls.glob("*.events.jsonl")).read_text() == '{"partial":true}\n'
        assert next(calls.glob("*.stderr.log")).read_text() == 'partial diagnostic'
        events = [json.loads(line) for line in (archive / "control/visible-events.jsonl").read_text().splitlines()]
        assert [e["type"] for e in events] == ["observation", "query", "response", "query", "infrastructure_exception"]
    report = {"passed": True, "model_calls": 0, "bridge_sha256": hashlib.sha256(bridge.read_bytes()).hexdigest(),
              "checks": ["observe forwards observation and next_query", "respond preserves native result", "native exception is recorded and re-raised", "timeout partial stdout and stderr survive"]}
    (ROOT / "reports/bridge-callback-check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
