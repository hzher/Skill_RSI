"""Task artifact transport enforces ownership and retains interrupted mutations."""
import base64
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from adapters import meal_plan_artifacts as artifacts
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile
from workenv.core import ResourceError, save


def payload():
    return {"meal_plan": [{"day": "wrong date is measurable", "meal_type": "breakfast", "recipe_name": "=Literal recipe", "prep_time": 15}],
            "shopping_list": [{"ingredient": "=Literal ingredient", "quantity": "as needed"}]}


class MealArtifactTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.profile = EpisodeProfile(self.root, "f1895a7d-8626-4818-839a-d5ddab57b4a0", 0)
        self.workspace = self.profile.base / "calendar-workspace"
        self.workspace.mkdir(parents=True)
        save(self.profile.base / "cluster_owner.json", self.profile.record())
        save(self.profile.base / "calendar-initialized.json", {"state": "ready", "profile": self.profile.record()})
        self.cluster = EpisodeCluster(self.profile)
        self.cluster.started = True  # An explicit fixture; no database process.

    def test_payload_structure_does_not_grade_task_correctness_or_accept_paths(self):
        self.assertEqual(len(artifacts.validate_payload(payload())), 64)
        with self.assertRaises(ValueError): artifacts.validate_payload({**payload(), "path": "elsewhere.xlsx"})
        bad = payload()
        bad["meal_plan"][0]["unexpected"] = "field"
        with self.assertRaises(ValueError): artifacts.validate_payload(bad)

    def test_nonfinite_huge_complex_and_unbounded_cells_are_rejected(self):
        for value in (float("nan"), float("inf"), 10**400, True, {}, [15]):
            data = payload()
            data["meal_plan"][0]["prep_time"] = value
            with self.assertRaises(ValueError): artifacts.validate_payload(data)
        for value in ("x" * 513, "illegal\x00cell"):
            data = payload()
            data["shopping_list"][0]["ingredient"] = value
            with self.assertRaises(ValueError): artifacts.validate_payload(data)
        data = payload()
        data["meal_plan"] *= 129
        with self.assertRaises(ValueError): artifacts.validate_payload(data)

    def test_foreign_closed_or_unready_episode_is_rejected(self):
        with self.assertRaises(ValueError): artifacts.workspace_for(self.root / "foreign", self.cluster)
        self.cluster.started = False
        with self.assertRaises(ResourceError): artifacts.workspace_for(self.root, self.cluster)
        self.cluster.started = True
        save(self.profile.base / "calendar-initialized.json", {"state": "initializing", "profile": self.profile.record()})
        with self.assertRaises(ResourceError): artifacts.workspace_for(self.root, self.cluster)

    def test_pending_or_foreign_intent_prevents_a_new_mutation(self):
        path = self.profile.base / "meal-artifact-write-fixture.json"
        for record in ({"state": "prepared", "profile": self.profile.record()}, {"state": "completed", "profile": {}}):
            save(path, record)
            original = path.read_bytes()
            with self.assertRaises(ResourceError): artifacts.require_no_pending_writes(self.cluster)
            self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(importlib.util.find_spec("openpyxl"), "Optional workbook dependency unavailable")
    def test_roundtrip_preserves_literals_and_previous_workbook_exact_bytes(self):
        from openpyxl import load_workbook
        first = artifacts.write_report(self.root, self.cluster, payload())
        downloaded = artifacts.read_report(self.root, self.cluster)
        raw = base64.b64decode(downloaded["base64"], validate=True)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), first["sha256"])
        workbook = load_workbook(self.workspace / "Weekly_Meal_Plan.xlsx", data_only=False)
        self.assertEqual(workbook["Meal Plan"]["C2"].data_type, "s")
        self.assertEqual(workbook["Meal Plan"]["C2"].value, "=Literal recipe")
        workbook.close()
        changed = payload()
        changed["meal_plan"][0]["recipe_name"] = "Another wrong recipe"
        second = artifacts.write_report(self.root, self.cluster, changed)
        self.assertEqual(second["previous_sha256"], first["sha256"])
        archive = self.profile.base / "meal-artifact-history" / (first["sha256"] + ".xlsx")
        self.assertEqual(archive.read_bytes(), raw)
        self.assertFalse(second["task_content_evaluated"])

    @unittest.skipUnless(importlib.util.find_spec("openpyxl"), "Optional workbook dependency unavailable")
    def test_failed_atomic_replace_preserves_original_and_blocks_retry(self):
        artifacts.write_report(self.root, self.cluster, payload())
        target = self.workspace / "Weekly_Meal_Plan.xlsx"
        original = target.read_bytes()
        with patch.object(artifacts, "replace_atomic", side_effect=PermissionError("injected replacement failure")):
            with self.assertRaises(PermissionError): artifacts.write_report(self.root, self.cluster, payload())
        self.assertEqual(target.read_bytes(), original)
        with self.assertRaises(ResourceError): artifacts.write_report(self.root, self.cluster, payload())
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(list(self.workspace.glob(".meal-workbook-*.xlsx")))
        intents = [json.loads(p.read_text()) for p in self.profile.base.glob("meal-artifact-write-*.json")]
        self.assertEqual(sorted(p["state"] for p in intents), ["completed", "prepared"])

    @unittest.skipUnless(importlib.util.find_spec("openpyxl"), "Optional workbook dependency unavailable")
    def test_write_budget_refuses_further_writes_but_completed_history_is_valid(self):
        for i in range(128):
            save(self.profile.base / ("meal-artifact-write-" + str(i) + ".json"), {"state": "completed", "profile": self.profile.record()})
        self.assertEqual(artifacts.require_no_pending_writes(self.cluster), 128)
        with self.assertRaises(ResourceError): artifacts.write_report(self.root, self.cluster, payload())
        self.assertFalse((self.workspace / "Weekly_Meal_Plan.xlsx").exists())

    def prepared_intent(self, *, previous=None, actual=None):
        """Explicit byte-state fixture; these bytes are not a task workbook."""
        fresh = b"new workbook bytes fixture"
        target = self.workspace / "Weekly_Meal_Plan.xlsx"
        if actual is not None: target.write_bytes(actual)
        elif target.exists(): target.unlink()
        record = {"schema_version": "meal-plan-artifact-write-v1", "profile": self.profile.record(),
                  "artifact_ref": target.relative_to(self.root).as_posix(), "bytes": len(fresh),
                  "sha256": hashlib.sha256(fresh).hexdigest(), "payload_sha256": "a" * 64,
                  "previous_sha256": hashlib.sha256(previous).hexdigest() if previous is not None else None,
                  "task_content_evaluated": False, "state": "prepared"}
        path = self.profile.base / ("meal-artifact-write-" + uuid4().hex + ".json")
        save(path, record)
        return path, target, fresh

    def test_observed_commit_preserves_intent_and_is_idempotent_without_replay(self):
        path, target, fresh = self.prepared_intent()
        target.write_bytes(fresh)
        original = path.read_bytes()
        self.cluster.started = False  # Metadata reconciliation also supports a stopped owned episode.
        result = artifacts.reconcile_write(self.root, self.cluster, path.name)
        self.assertEqual(result["decision"], "committed")
        self.assertFalse(result["replay_performed"])
        self.assertEqual(target.read_bytes(), fresh)
        self.assertEqual((self.root / result["original_intent_ref"]).read_bytes(), original)
        self.assertEqual(artifacts.require_no_pending_writes(self.cluster), 1)
        resolved = path.read_bytes()
        again = artifacts.reconcile_write(self.root, self.cluster, path.name)
        self.assertEqual(again["decision"], "already_resolved")
        self.assertEqual(path.read_bytes(), resolved)

    def test_observed_noncommit_preserves_old_or_missing_artifact_and_allows_progress(self):
        for previous in (None, b"old workbook bytes fixture"):
            path, target, _ = self.prepared_intent(previous=previous, actual=previous)
            original = path.read_bytes()
            result = artifacts.reconcile_write(self.root, self.cluster, path.name)
            self.assertEqual((result["decision"], result["state"]), ("not_committed", "aborted"))
            self.assertEqual(target.read_bytes() if target.exists() else None, previous)
            self.assertEqual((self.root / result["original_intent_ref"]).read_bytes(), original)
            artifacts.require_no_pending_writes(self.cluster)

    def test_unrecognized_bytes_keep_intent_and_artifact_unknown(self):
        path, target, _ = self.prepared_intent(previous=b"old", actual=b"unrecognized bytes")
        original = path.read_bytes()
        with self.assertRaises(ResourceError): artifacts.reconcile_write(self.root, self.cluster, path.name)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(target.read_bytes(), b"unrecognized bytes")
        with self.assertRaises(ResourceError): artifacts.require_no_pending_writes(self.cluster)

    def test_reconciliation_cannot_target_arbitrary_paths_or_rebound_artifacts(self):
        path, target, _ = self.prepared_intent()
        with self.assertRaises(ValueError): artifacts.reconcile_write(self.root, self.cluster, "../" + path.name)
        record = json.loads(path.read_text())
        record["artifact_ref"] = "outside.xlsx"
        save(path, record)
        original = path.read_bytes()
        with self.assertRaises(ResourceError): artifacts.reconcile_write(self.root, self.cluster, path.name)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(target.exists())

    def test_dead_writer_lock_is_archived_and_alive_or_unknown_lock_is_preserved(self):
        path, target, _ = self.prepared_intent(previous=b"old", actual=b"old")
        lock = self.profile.base / ".meal-artifact.lock"
        record = {"schema_version": "workenv-lock-v0.1", "workspace_owner": str(self.root),
                  "resource": lock.relative_to(self.root).as_posix(), "kind": "meal_plan_artifact", "process": {"pid": 12345}}
        raw = json.dumps(record).encode()
        lock.write_bytes(raw)
        for status in ("alive", "unknown"):
            with patch.object(artifacts, "process_identity", return_value={"status": status}):
                with self.assertRaises(ResourceError): artifacts.reconcile_write(self.root, self.cluster, path.name, recover_stale_lock=True)
            self.assertEqual(lock.read_bytes(), raw)
        with patch.object(artifacts, "process_identity", return_value={"status": "not_found"}):
            result = artifacts.reconcile_write(self.root, self.cluster, path.name, recover_stale_lock=True)
        self.assertEqual((self.root / result["recovered_lock_ref"]).read_bytes(), raw)
        self.assertFalse(lock.exists())
        self.assertEqual(target.read_bytes(), b"old")
