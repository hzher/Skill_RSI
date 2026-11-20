"""Standalone reader checks plus an integration test against a local CTX protocol checkout."""
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from skillrl.codex_chatgpt import execute
from skillrl.export_ctx import artifact_name, export, view


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(root, *, stateful=False):
    attempt = "attempt-b" if stateful else "attempt-a"
    case = root / ("stateful" if stateful else "fresh") / "cases" / "001-example"
    identity = {"task_id": "example-000", "attempt_id": attempt, "canonical_index": 0,
                "family": "example", "instance_id": "item0", "schedule": "default"}
    put(case / "identity.json", identity)
    put(case / "initial-task.json", {"query": "Test instruction"})
    put(case / "actor/prompt.txt", "Test instruction")
    cli = case / "actor/native-cli.jsonl"
    cli.write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}) + "\n")
    result = {"content": [{"text": json.dumps({"action": {"pick": 1}, "observation": "ok"})}], "isError": False}
    with (case / "broker.jsonl").open("w", encoding="utf-8") as broker:
        for seq in (1, 2):
            call = {"sequence": seq, "timestamp": 1, "operation_id": "submit1", "tool": "act",
                    "arguments": {"action": {"pick": 1}}, "cached_receipt": seq == 2}
            broker.write(json.dumps({**call, "type": "tool_intent"}) + "\n")
            broker.write(json.dumps({**call, "type": "tool_result", "result": result}) + "\n")
    put(case / "native-objective.json", {"instance_outcomes": [{"instance_id": "item0", "instance_index": 0,
        "success": True, "reward": 0.7, "metadata": {"answer": "private-scorer-fixture"}}]})
    put(case / "workspace-final" / "output.json", {"submitted": True})
    put(case / "workspace-final" / "private.txt", "private-workspace-fixture")
    workspace_file = case / "workspace-final" / "output.json"
    put(case / "workspace-capture.json", {"files": {"output.json": {"sha256": sha(workspace_file),
                                                                    "bytes": workspace_file.stat().st_size}}})
    if stateful:
        put(case / "continuity.json", {"attempt_id": attempt, "family": "example", "canonical_index": 0,
            "previous_attempt_id": "older-attempt", "thread_id": "thread", "resumed_thread_id": "thread",
            "session_started_here": False, "initialization_terminal": False})
    names = ["initial-task.json", "actor/prompt.txt", "actor/native-cli.jsonl", "broker.jsonl", "workspace-capture.json"]
    if stateful:
        names.append("continuity.json")
    put(case / "artifact-manifest.json", {name: {"bytes": (case / name).stat().st_size, "sha256": sha(case / name)}
                                          for name in names})
    manifest = sha(case / "artifact-manifest.json")
    put(case / "scoring-link.json", {"identity": identity, "mapping": "exact", "query_instance_id": "item0",
        "outcome_instance_id": "item0", "artifact_manifest_sha256": manifest,
        "verifier_receipt_sha256": sha(case / "native-objective.json")})
    record = {"identity": identity, "score_binding_version": 1, "score": 1, "family": "example",
        "artifact_manifest_sha256": manifest, "complete": True, "native_thread_ids": ["thread"],
        "native": {"usage": {"input_tokens": 100}}, "resource_closed": True,
        "configuration": {"model": "test-model", "auth_mode": "chatgpt", "secret": "private-config-fixture"}}
    if stateful:
        record.update(execution_mode="stateful", usage_delta={"input_tokens": 12}, usage_delta_scope="per_attempt_delta")
    put(case / "result.json", record)
    return case


class ExportTests(unittest.TestCase):
    def test_version_removal_does_not_reclassify_bound_receipts_as_legacy(self):
        with TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            record = json.loads((case / "result.json").read_bytes())
            del record["score_binding_version"]
            objective = json.loads((case / "native-objective.json").read_bytes())
            objective["instance_outcomes"][0]["reward"] += 1
            put(case / "native-objective.json", objective)
            put(case / "result.json", record)
            with self.assertRaisesRegex(ValueError, "Missing score binding version"):
                view(case)
            (case / "scoring-link.json").unlink()
            with self.assertRaisesRegex(ValueError, "Missing score binding version"):
                view(case)

    def test_legacy_outcome_requires_matching_collector_and_native_receipts(self):
        with TemporaryDirectory() as tmp:
            case = Path(tmp)
            identity = {"task_id": "legacy", "attempt_id": "older", "instance_id": "item0"}
            objective = {"instance_outcomes": [{"instance_id": "item0", "success": True, "reward": .3}]}
            put(case / "identity.json", identity)
            put(case / "result.json", {"identity": identity, "score": 1, "objective": objective, "configuration": {}})
            put(case / "native-objective.json", objective)
            self.assertEqual(view(case)[1]["score_mapping"], "legacy_exact_id")
            objective["instance_outcomes"][0]["reward"] += 1
            put(case / "native-objective.json", objective)
            with self.assertRaisesRegex(ValueError, "Legacy identity or original outcome"):
                view(case)

    def test_score_is_bound_to_manifest_receipt_and_attempt(self):
        with TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            public, score = view(case)
            self.assertEqual((score["score"], score["native_reward"]), (1, 0.7))
            self.assertNotIn("private-", json.dumps([public, score]))
            self.assertFalse((case / "evidence-view").exists())
            for name in ("artifact-manifest.json", "native-objective.json", "identity.json"):
                with self.subTest(name=name):
                    path = case / name
                    before = path.read_bytes()
                    put(path, {})
                    with self.assertRaises(ValueError):
                        view(case)
                    path.write_bytes(before)

    def test_rejects_unbounded_artifact_paths(self):
        self.assertEqual("outputs/final.csv", artifact_name("outputs/final.csv"))
        for item in ("../escape", "/private/key", "C:/outside", ".ssh/id", "x/../auth.json", "a\\b",
                     "state/results.sqlite3", "a/.env", ""):
            with self.subTest(item=item), self.assertRaises(ValueError):
                artifact_name(item)

    @unittest.skipUnless(os.name == "posix" and os.environ.get("SKILL_RSI_CTX_ROOT"),
                         "Set SKILL_RSI_CTX_ROOT to a local SkillRSI-CTX-v1 checkout on Linux")
    def test_failed_evaluator_emits_only_bound_diagnostics_and_no_score(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "input"
            case = fixture(root)
            (case / "native-objective.json").unlink()
            failure = {"type": "RuntimeError", "message": "private-verifier-traceback " + str(root)}
            failure_path = case / "scoring-failure.json"
            put(failure_path, failure)
            link = json.loads((case / "scoring-link.json").read_bytes())
            link.update(mapping="native_evaluator_failed", outcome_instance_id=None,
                        verifier_receipt_sha256=None, evaluation_failure_sha256=sha(failure_path))
            put(case / "scoring-link.json", link)
            record = json.loads((case / "result.json").read_bytes())
            record.update(complete=False, score=None, scoring_status="unavailable",
                          score_mapping="native_evaluator_failed", native_outcome_instance_id=None)
            put(case / "result.json", record)
            out = Path(tmp) / "out"
            report = export(root, [case.parents[1]], out, os.environ["SKILL_RSI_CTX_ROOT"])
            self.assertEqual((report["integrity"], report["run_count"], report["pre_identity_failure_count"]), ("verified", 1, 0))
            score = json.loads((out / "runs/attempt-a/scores.jsonl").read_bytes())
            self.assertEqual(score["evaluation_failure_sha256"], sha(failure_path))
            self.assertEqual(score["evaluation_failure_type"], "RuntimeError")
            self.assertEqual(score["grading_status"], "unscorable")
            for key in ("raw_score", "native_success", "shared_score", "private_verifier_receipt_sha256"):
                self.assertIsNone(score[key])
            self.assertFalse(any(b"private-verifier-traceback" in path.read_bytes() for path in out.rglob("*") if path.is_file()))
            put(case / "native-objective.json", {"instance_outcomes": []})
            with self.assertRaisesRegex(ValueError, "Unverified native evaluator failure"):
                view(case)
            (case / "native-objective.json").unlink()
            put(failure_path, {**failure, "message": "changed"})
            with self.assertRaisesRegex(ValueError, "Unverified native evaluator failure"):
                view(case)
            del record["score_binding_version"]
            put(case / "result.json", record)
            with self.assertRaisesRegex(ValueError, "Missing score binding version"):
                view(case)

    @unittest.skipUnless(os.name == "posix" and os.environ.get("SKILL_RSI_CTX_ROOT"),
                         "Set SKILL_RSI_CTX_ROOT to a local SkillRSI-CTX-v1 checkout on Linux")
    def test_pre_identity_failures_are_counted_beside_sealed_trajectories(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "input"
            fresh = fixture(root)
            failed = root / "failed/cases/001-example"
            config = {"model": "test", "cli": "unused", "network": "unused", "repository": str(root)}
            with patch("skillrl.clbench.CLBenchTask", side_effect=RuntimeError("private-traceback " + str(root))):
                execute(config, "example", 0, failed)
            protocol = os.environ["SKILL_RSI_CTX_ROOT"]
            for include_success in (True, False):
                out = Path(tmp) / ("mixed" if include_success else "all-failed")
                collections = [failed.parents[1]] + ([fresh.parents[1]] if include_success else [])
                result = export(root, collections, out, protocol)
                self.assertEqual((result["integrity"], result["run_count"], result["pre_identity_failure_count"]),
                                 ("verified", int(include_success), 1))
                index = (out / "batch_index.json").read_bytes()
                coverage = json.loads(index)["harness_imports"]["codex"]["coverage"]
                self.assertEqual((coverage["sealed_case_count"], coverage["trajectory_count"]), (len(collections), int(include_success)))
                diagnostic, = coverage["pre_identity_failures"]
                self.assertEqual(diagnostic["case"], failed.relative_to(root).as_posix())
                self.assertEqual(diagnostic["exception_type"], "RuntimeError")
                self.assertEqual(diagnostic["receipt_sha256"]["result.json"], sha(failed / "result.json"))
                self.assertNotIn(b"private-traceback", index)
                self.assertNotIn(str(root).encode(), index)
                self.assertEqual(len(list((out / "runs").iterdir())), int(include_success))
                self.assertEqual(export(root, collections, out, protocol), result)
            (failed / "broker.jsonl").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unverified pre-identity"):
                export(root, [failed.parents[1]], Path(tmp) / "invalid", protocol)
            self.assertFalse((Path(tmp) / "invalid").exists())

    @unittest.skipUnless(os.name == "posix" and os.environ.get("SKILL_RSI_CTX_ROOT"),
                         "Set SKILL_RSI_CTX_ROOT to a local SkillRSI-CTX-v1 checkout on Linux")
    def test_fresh_and_stateful_publish_and_verify(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "input"
            fresh = fixture(root)
            stateful = fixture(root, stateful=True)
            output = Path(tmp) / "output"
            collections = [fresh.parents[1], stateful.parents[1]]
            def publish(paths=collections):
                return export(root, paths, output, os.environ["SKILL_RSI_CTX_ROOT"], artifacts=["output.json"])
            self.assertEqual(publish()["run_count"], 2)
            first = (output / "batch_index.json").read_bytes()
            self.assertEqual(publish()["integrity"], "verified")
            self.assertEqual(first, (output / "batch_index.json").read_bytes())
            with self.assertRaisesRegex(ValueError, "different evidence"):
                publish(collections[:1])
            with self.assertRaisesRegex(ValueError, "Duplicate attempt"):
                publish([collections[0], collections[0]])
            self.assertEqual(first, (output / "batch_index.json").read_bytes())
            for attempt, phase in [("attempt-a", "independent"), ("attempt-b", "stateful")]:
                run = output / "runs" / attempt
                manifest = json.loads((run / "manifest.json").read_bytes())
                self.assertEqual(manifest["phase"], phase)
                events = [json.loads(row) for row in (run / "events.jsonl").read_bytes().splitlines()]
                self.assertEqual(len([row for row in events if row["type"] == "environment_action"]), 1)
                request = json.loads((run / "requests/index.jsonl").read_bytes())
                self.assertEqual(request["call_granularity"], "cli_invocation")
                self.assertIsNone(request["actual_context_ref"])
                self.assertIsNone(request["attempt_id"])
                if phase == "stateful":
                    self.assertEqual(manifest["previous_attempt_id"], "older-attempt")
                    self.assertEqual(request["usage_refs"][0]["locator"]["pointer"], "/usage_delta")
                    self.assertEqual(request["thread_cumulative_usage_refs"][0]["locator"]["pointer"], "/native/usage")
                published = b"".join(p.read_bytes() for p in run.rglob("*") if p.is_file())
                for sentinel in (b"private-workspace-fixture", b"private-scorer-fixture", b"private-config-fixture"):
                    self.assertNotIn(sentinel, published)
                self.assertIn(b"output.json", published)
            (fresh / "broker.jsonl").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differs from manifest"):
                publish()
            self.assertEqual(first, (output / "batch_index.json").read_bytes())
