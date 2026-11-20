import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from skillrl.collect import run_collection, select_tasks
from skillrl.clbench import CLBenchTask, remove_owned_container


def plan():
    return {"tasks": [{"id": f"scan-{i}", "family": "blind_spectrum_monitoring", "canonical_index": i}
                      for i in range(2)]}


class CollectionEntryTests(unittest.TestCase):
    def test_original_evaluator_failure_is_bound_and_not_retried(self):
        from skillrl.codex_chatgpt import digest, execute
        from test_codex_chatgpt import ExampleTask

        with TemporaryDirectory() as tmp:
            case = Path(tmp) / "case"
            task = ExampleTask(None, None, 0, None)
            task.done = True
            config = {"backend": "codex", "repository": tmp, "model": "fixture", "network": "unused"}
            with patch("skillrl.clbench.CLBenchTask", return_value=task), \
                    patch.object(task, "outcome", side_effect=RuntimeError("evaluator unavailable")) as evaluator:
                result = execute(config, "sample", 0, case)
                self.assertEqual(result, execute(config, "sample", 0, case))
            self.assertEqual(evaluator.call_count, 1)
            self.assertTrue(result["resource_closed"])
            self.assertTrue(result["execution_complete"])
            self.assertFalse(result["complete"])
            self.assertIsNone(result["score"])
            self.assertFalse((case / "native-objective.json").exists())
            link = json.loads((case / "scoring-link.json").read_bytes())
            self.assertEqual(link["mapping"], "native_evaluator_failed")
            self.assertEqual(link["evaluation_failure_sha256"], digest(case / "scoring-failure.json"))

    def test_actor_failure_after_terminal_action_still_binds_native_score(self):
        from skillrl.codex_chatgpt import execute
        from test_codex_chatgpt import ExampleTask

        def lost_exit(config, path, task, owner, prompt):
            owner.submit(task.act, {"transmitters": []}).result()
            raise RuntimeError("Missing host exit receipt")

        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "case"
            config = {"backend": "codex", "repository": tmp, "model": "fixture", "network": "unused"}
            with patch("skillrl.clbench.CLBenchTask", side_effect=ExampleTask), \
                    patch("skillrl.codex_chatgpt.run_actor", side_effect=lost_exit) as actor:
                result = execute(config, "sample", 4, out)
                self.assertEqual(result, execute(config, "sample", 4, out))
            self.assertEqual(actor.call_count, 1)
            self.assertTrue(result["resource_closed"])
            self.assertFalse(result["execution_complete"])
            self.assertFalse(result["complete"])
            self.assertEqual(result["task_actions"], 1)
            self.assertEqual(result["score"], 0)
            self.assertEqual(result["failure"]["message"], "Missing host exit receipt")
            self.assertTrue((out / "scoring-link.json").is_file())

    def test_reset_failure_closes_the_owned_database(self):
        import sqlite3
        import tempfile
        from types import ModuleType
        registry = ModuleType("src.registry")
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
            database = Path(handle.name)
        self.addCleanup(database.unlink, missing_ok=True)
        native = SimpleNamespace(_conn=sqlite3.connect(database), _temp_db_path=database)
        native.reset_baseline_instance = lambda index: None
        native.canonical_instance_index = lambda index: -1
        registry.get_task_class = lambda family: lambda schedule: native
        with patch.dict(sys.modules, {"src.registry": registry}), patch.object(sys, "path", sys.path.copy()):
            with self.assertRaisesRegex(ValueError, "instance selection"):
                CLBenchTask(".", "synthetic", 1, "unused")
        self.assertIsNone(native._conn)
        self.assertFalse(database.exists())

    def test_container_cleanup_requires_one_owned_id(self):
        with patch("skillrl.clbench.docker", return_value=SimpleNamespace(stdout=b"bad-id")) as docker:
            with self.assertRaisesRegex(ValueError, "container ID"):
                remove_owned_container("name")
            docker.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
                remove_owned_container("a" * 12)
            self.assertEqual(docker.call_count, 1)

    def test_rejected_modes_create_no_run_or_model_calls(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            for backend, mode in (("codex", "shuffled"), ("claude", "stateful")):
                with self.subTest(backend=backend), self.assertRaises(ValueError):
                    run_collection({"backend": backend, "repository": tmp, "model": "fixture"}, plan(), mode, output)
            self.assertFalse(output.exists())
        with self.assertRaises(ValueError):
            select_tasks({"tasks": list(reversed(plan()["tasks"]))}, "stateful")

    def test_independent_keeps_plan_task_ids_and_stops_on_capacity(self):
        with TemporaryDirectory() as tmp:
            config = {"backend": "codex", "model": "fixture", "repository": tmp, "stop_on_account_capacity": True}
            limited = {"complete": False, "auth_mode": "chatgpt", "native": {"errors": ["rate_limit"]}}
            with patch("skillrl.codex_chatgpt.execute", return_value=limited) as run:
                result = run_collection(config, plan(), "independent", Path(tmp) / "output")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs["task_id"], "scan-0")
            self.assertFalse(result["complete"])
            self.assertEqual(result["stop_reason"], "account_capacity_unavailable")
            self.assertEqual(result["unstarted_tasks"], plan()["tasks"][1:])

    def test_stateful_low_disk_stops_before_new_family_intent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            config = {"backend": "codex", "model": "fixture", "repository": tmp, "min_free_bytes": 100}
            with patch("skillrl.codex_stateful.shutil.disk_usage", return_value=SimpleNamespace(free=50)):
                result = run_collection(config, plan(), "stateful", root)
            self.assertEqual(result["stop_reason"], "insufficient_free_disk_for_evidence")
            self.assertEqual(result["unstarted_tasks"], plan()["tasks"])
            self.assertFalse((root / "families").exists())

    @unittest.skipUnless(os.environ.get("CLBENCH_REPOSITORY") and os.environ.get("SKILL_RSI_CTX_ROOT"),
                         "Set CLBENCH_REPOSITORY and SKILL_RSI_CTX_ROOT to test with the upstream checkouts")
    def test_real_benchmark_mcp_and_ctx_from_cli(self):
        framework = Path(__file__).resolve().parents[1]

        def cli(module, *arguments):
            result = subprocess.run([sys.executable, "-B", "-m", module, *map(str, arguments)],
                                    cwd=framework, text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return json.loads(result.stdout.splitlines()[-1])

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "scripted-codex"
            # The collector is POSIX based. Transcode the fixture for CRLF checkouts on WSL.
            binary.write_text(Path(__file__).with_name("scripted_codex_cli.py").read_text(), newline="\n")
            binary.chmod(0o700)
            cfg, manifest = root / "config.json", root / "plan.json"
            cfg.write_text(json.dumps({"backend": "codex", "auth_mode": "chatgpt", "model": "test-model",
                                       "cli": str(binary), "repository": os.environ["CLBENCH_REPOSITORY"],
                                       "network": "unused-in-memory"}))
            manifest.write_text(json.dumps(plan()))
            for mode, sessions in (("independent", 2), ("stateful", 1)):
                out = root / mode
                params = ["--config", cfg, "--plan", manifest, "--mode", mode]
                self.assertEqual(cli("skillrl.collect", *params, "--check")["planned_task_sessions"], sessions)
                self.assertTrue(cli("skillrl.collect", *params, "--output", out)["complete"])
                cases = sorted((out / "cases").iterdir())
                receipts = [(case / "result.json").read_bytes() for case in cases]
                records = [json.loads(raw) for raw in receipts]
                self.assertEqual([r["identity"]["task_id"] for r in records], ["scan-0", "scan-1"])
                self.assertEqual(len({r["native_thread_ids"][0] for r in records}), sessions)
                for case, record in zip(cases, records, strict=True):
                    self.assertTrue(record["resource_closed"])
                    self.assertEqual(record["task_actions"], 1)
                    outcome, = json.loads((case / "native-objective.json").read_bytes())["instance_outcomes"]
                    self.assertEqual(record["score"], int(outcome["success"]))
                    journal = [json.loads(line) for line in (case / "broker.jsonl").read_bytes().splitlines()]
                    returned = [r for r in journal if r["type"] == "tool_result"]
                    self.assertEqual([r["tool"] for r in returned], ["status", "act"])
                    self.assertNotIn("instance_id", json.loads(returned[0]["result"]["content"][0]["text"]))
                self.assertTrue(cli("skillrl.collect", *params, "--output", out)["complete"])
                self.assertEqual(receipts, [(case / "result.json").read_bytes() for case in cases])
                dest = root / (mode + "-ctx")
                cli("skillrl.export_ctx", "--root", root, "--collection", out, "--output", dest,
                    "--protocol-root", os.environ["SKILL_RSI_CTX_ROOT"])
                self.assertEqual(len(json.loads((dest / "batch_index.json").read_bytes())["runs"]), 2)
