import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from skillrl import codex_chatgpt as backend
from skillrl.codex_host_actor import actor_arguments
from skillrl.interactive import ActionValidationError


SCRIPTED_CLI = Path(__file__).with_name('scripted_codex_cli.py')


class ExampleTask:
    def __init__(self, repository, family, index, network):
        self.task = SimpleNamespace()
        self.query = SimpleNamespace(instance_id="native-id")
        self.container, self.cwd = None, "/workspace"
        self.done, self.actions, self.container_adaptations = False, 0, []

    def status(self):
        return {"done": self.done, "cwd": self.cwd, "query": "Finish the example"}

    def act(self, action):
        if action != {"transmitters": []}:
            raise ActionValidationError("Unknown action")
        self.actions += 1
        self.done = True
        return self.status()

    def outcome(self):
        return {"instance_outcomes": [{"instance_id": "native-id", "instance_index": 0,
                "success": False, "reward": 0.25}]}

    def close(self):
        pass


class CodexTests(unittest.TestCase):
    def test_local_cli_mcp_receipt_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / "codex"
            cli.write_text(SCRIPTED_CLI.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            cli.chmod(0o755)
            config = {"cli": str(cli), "model": "fixture", "repository": str(root), "network": "unused"}
            path = root / "run"
            with patch("skillrl.clbench.CLBenchTask", wraps=ExampleTask) as factory, \
                    patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-value"}):
                first = backend.execute(config, "sample", 8, path, task_id="catalog-id")
                self.assertTrue(first["complete"], (first, (path / "host-launch.stderr").read_text()))
                second = backend.execute(config, "sample", 8, path, task_id="catalog-id")
            self.assertEqual(second, first)
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(first["score"], 0)  # Positive native reward with a negative verifier decision.
            self.assertEqual(first["identity"]["task_id"], "catalog-id")
            self.assertEqual(first["task_actions"], 1)
            self.assertEqual(first["mcp_calls"], 2)
            self.assertTrue(first["resource_closed"])
            self.assertEqual(len(first["native_thread_ids"]), 1)
            self.assertEqual(str(uuid.UUID(first["native_thread_ids"][0])), first["native_thread_ids"][0])
            self.assertEqual(json.loads((path / "scoring-link.json").read_bytes())["artifact_manifest_sha256"],
                             backend.digest(path / "artifact-manifest.json"))
            self.assertNotIn("fixture-value", (path / "actor/actor-config.json").read_text())
            self.assertNotIn("http://", (path / "actor/actor-config.json").read_text())

    def test_automatic_task_scores_once_without_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = ExampleTask(None, None, None, None)
            task.done = True
            with patch("skillrl.clbench.CLBenchTask", return_value=task), patch.object(task, "outcome", wraps=task.outcome) as score:
                for _ in range(2):
                    r = backend.execute({"repository": tmp, "network": "unused", "model": "unused"}, "example", 0, tmp)
                    self.assertTrue(r["complete"])
                    self.assertTrue(r["initialization_terminal"])
                self.assertEqual(score.call_count, 1)
                self.assertFalse((Path(tmp) / "actor").exists())

    def test_no_api_or_inline_credentials(self):
        for settings in ({"auth_mode": "api"}, {"api_key": "fixture"}, {"model_provider": "other"}):
            with self.subTest(settings=list(settings)), self.assertRaises(ValueError):
                backend.validate_config({"model": "fixture", **settings})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / "codex"
            cli.write_text("#!/usr/bin/env python3\nprint('Logged in using an API key: fixture')\n")
            cli.chmod(0o755)
            config = {"cli": str(cli), "model": "fixture", "repository": tmp, "network": "unused"}
            with patch("skillrl.clbench.CLBenchTask", ExampleTask):
                result = backend.execute(config, "example", 8, root / "run")
            self.assertFalse(result["complete"])
            self.assertIsNone(result["score"])
            self.assertTrue(result["resource_closed"])
            self.assertFalse((root / "run/actor/native-cli.jsonl").exists())

    def test_score_binds_singleton_identity_without_loosening_stateful_indices(self):
        identity = {"canonical_index": 8, "instance_id": "native-id"}
        outcome = {"instance_id": "native-id", "instance_index": 0, "success": True}
        obj = {"instance_outcomes": [outcome]}
        self.assertEqual(backend.match_outcome(obj, identity)[1], "unmatched")
        identity["selected_index"] = 8
        self.assertEqual(backend.match_outcome(obj, identity)[1], "exact")
        outcome["instance_index"] = 7
        self.assertEqual(backend.match_outcome(obj, identity)[1], "unmatched")
        outcome.update(instance_index=0, instance_id="different")
        self.assertEqual(backend.match_outcome(obj, identity)[1], "unmatched")
        obj["instance_outcomes"].append(dict(outcome))
        self.assertEqual(backend.match_outcome(obj, identity)[1], "unmatched")

    def test_repeated_operations_keep_one_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = ExampleTask(None, None, None, None)
            path = Path(tmp)
            broker = backend.JournalBroker(task, path / "tools", journal=path / "broker.jsonl")
            self.assertTrue(broker.call("bad", "act", {"action": {}})["isError"])
            args = {"action": {"transmitters": []}}
            self.assertEqual(broker.call("ok", "act", args), broker.call("ok", "act", args))
            self.assertEqual(task.actions, 1)
            self.assertTrue(backend.read_events(path / "broker.jsonl")[0][-1]["cached_receipt"])
            with self.assertRaisesRegex(ValueError, "different inputs"):
                broker.call("ok", "act", {"action": {}})

    def test_resumed_family_uses_same_workspace_and_explicit_thread(self):
        spec = {"cli": "codex", "model": "fixture", "mcp": {"command": "fixture"},
                "output": "/tmp/example", "family_workspace": "/tmp/family"}
        first = actor_arguments(spec)
        self.assertNotIn("--ephemeral", first)
        token = str(uuid.uuid4())
        resumed = actor_arguments({**spec, "resume_thread_id": token})
        self.assertEqual(resumed[-2:], [token, "-"])
        self.assertLess(resumed.index("-C"), resumed.index("resume"))
        with self.assertRaises(ValueError):
            actor_arguments({**spec, "resume_thread_id": "--last"})

    def test_missing_tool_return_or_undeclared_tool_never_completes(self):
        item = {"type": "mcp_tool_call", "id": "c1", "server": "benchmark", "tool": "act"}
        events = [{"type": "item.started", "item": item}, {"type": "turn.completed"}]
        self.assertFalse(backend.summarize_native(events, 0)["complete"])
        events.append({"type": "item.completed", "item": item})
        self.assertTrue(backend.summarize_native(events, 0)["complete"])
        events.append({"type": "item.completed", "item": {"type": "command_execution"}})
        self.assertFalse(backend.summarize_native(events, 0)["complete"])


if __name__ == "__main__":
    unittest.main()
