"""Resource isolation, lifecycle and genuine verifier semantics in temporary worlds."""
from copy import deepcopy
from pathlib import Path
import tempfile
import hashlib
import json
import unittest
from unittest.mock import patch
import uuid

from integrations import synthetic_backend as backend
from integrations.resource_ledger import ResourceLedger
from integrations.synthetic_audit import audit_synthetic, required_checks
from workenv import core, file_adapter, taskpacks
from workenv.core import save
from workenv.office_logic import reference
from workenv.ticket_logic import reference_tickets


class SyntheticResourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nemo-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for module in (core, file_adapter, taskpacks, backend):
            item = patch.object(module, "ROOT", self.root)
            item.start()
            self.addCleanup(item.stop)
        save(self.root / "tasks/registry.json", {"tasks": []})
        self.entries = [taskpacks.write_pack(taskpacks.pack(f, 11, "base", "dev"))
                        for f in ("receivables", "meeting", "documents", "tickets")]
        save(self.root / "tasks/registry.json", {"tasks": self.entries})
        self.clock = [1000.0]
        self.ledger = ResourceLedger(self.root / "ledger.json", self.root, clock=lambda: self.clock[0])
        self.resources = backend.SyntheticResources(self.ledger)
        self.addCleanup(self.resources.close_all)

    def seed(self, family="documents"):
        body = {"resources_session_id": str(uuid.uuid4()), "episode_id": {"rollout_id": uuid.uuid4().hex, "attempt": 0},
                "task_id": {"taskset": "project_synthetic:diagnostic", "task_id": f"local-{family}-11-base"}, "task_data": {}}
        self.resources.dispatch("/seed_session", body)
        return body

    def bound(self, seed):
        return {k: seed[k] for k in ("resources_session_id", "episode_id")}

    def call(self, seed, name, args, rid=None):
        return self.resources.dispatch("/tool", {**self.bound(seed), "request_id": rid or str(uuid.uuid4()),
                                                 "name": name, "arguments": args})["result"]

    def test_foreign_sources_and_nonempty_seed_data_never_create_resources(self):
        body = {"resources_session_id": str(uuid.uuid4()), "episode_id": {"rollout_id": "test"},
                "task_id": {"taskset": "toolathlon_gym:diagnostic", "task_id": "local-documents-11-base"}, "task_data": {}}
        with self.assertRaises(ValueError): self.resources.dispatch("/seed_session", body)
        body["task_id"]["taskset"] = "project_synthetic:diagnostic"
        self.entries[0]["source"] = "toolathlon_gym"
        save(self.root / "tasks/registry.json", {"tasks": self.entries})
        body["task_id"]["task_id"] = self.entries[0]["task_id"]
        with self.assertRaises(ValueError): self.resources.dispatch("/seed_session", body)
        self.assertEqual(self.ledger.rows, {})

    def test_seed_episode_binding_and_closed_tombstone_are_enforced(self):
        body = self.seed()
        self.assertEqual(self.resources.dispatch("/seed_session", body), {"resources_session_id": body["resources_session_id"]})
        changed = deepcopy(body)
        changed["episode_id"]["attempt"] = 1
        with self.assertRaises(backend.SyntheticConflict): self.resources.dispatch("/seed_session", changed)
        with self.assertRaises(backend.SyntheticConflict): self.resources.dispatch("/tools", self.bound(changed))
        self.resources.dispatch("/close_session", self.bound(body))
        self.resources.dispatch("/close_session", self.bound(body))
        with self.assertRaises(backend.SyntheticConflict): self.resources.dispatch("/seed_session", body)

    def test_two_resources_never_share_workspaces_or_mutations(self):
        first, second = self.seed(), self.seed()
        self.call(first, "write_file", {"path": "output/only-first.txt", "content": "isolated"})
        self.assertIn("output/only-first.txt", self.call(first, "list_files", {}))
        self.assertNotIn("output/only-first.txt", self.call(second, "list_files", {}))
        with self.assertRaises(ValueError): self.call(first, "read_file", {"path": "../private/task.md"})
        self.assertFalse(self.resources.dispatch("/tools", self.bound(first))["training_eligible"])

    def test_mutations_replay_once_and_unknown_outcomes_are_never_retried(self):
        body = self.seed()
        rid = str(uuid.uuid4())
        args = {"path": "output/x.txt", "content": "one"}
        self.assertEqual(self.call(body, "write_file", args, rid), self.call(body, "write_file", args, rid))
        with self.assertRaises(backend.SyntheticConflict): self.call(body, "write_file", {**args, "content": "two"}, rid)
        missing = str(uuid.uuid4())
        with self.assertRaises(FileNotFoundError): self.call(body, "read_file", {"path": "inputs/missing"}, missing)
        with self.assertRaises(backend.SyntheticConflict): self.call(body, "read_file", {"path": "inputs/missing"}, missing)

    def test_all_families_measure_real_failure_then_independent_success(self):
        for family in ("receivables", "meeting", "documents", "tickets"):
            body = self.seed(family)
            negative = self.resources.dispatch("/verify", self.bound(body))
            self.assertEqual((negative["reward"], negative["mask_sample"]), (0.0, False))
            if family == "tickets":
                class Tools:
                    def call(inner, name, args): return self.call(body, name, args)
                outputs = reference_tickets(Tools())
            else:
                inputs = {p.removeprefix("inputs/"): self.call(body, "read_file", {"path": p})
                          for p in self.call(body, "list_files", {}) if p.startswith("inputs/")}
                outputs = reference(family, inputs)
            for name, content in outputs.items(): self.call(body, "write_file", {"path": "output/" + name, "content": content})
            positive = self.resources.dispatch("/verify", self.bound(body))
            self.assertEqual((positive["reward"], positive["mask_sample"], positive["source"], positive["training_eligible"]),
                             (1.0, False, "project_synthetic", False))
            self.assertTrue((self.root / positive["evaluation_ref"]).is_file())
            self.resources.dispatch("/close_session", self.bound(body))

    def test_verifier_fault_does_not_become_a_measured_zero(self):
        body = self.seed()
        session = self.resources.sessions[body["resources_session_id"]]["session"]
        with patch.object(session.adapter, "evaluate", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError): self.resources.dispatch("/verify", self.bound(body))

    def test_lease_closes_resources_and_persistent_tombstone_survives_restart(self):
        body = self.seed()
        self.clock[0] += 901
        self.resources.reap()
        reloaded = backend.SyntheticResources(ResourceLedger(self.root / "ledger.json", self.root))
        reloaded.recover()
        with self.assertRaises(backend.SyntheticConflict): reloaded.dispatch("/seed_session", body)
        self.assertEqual(reloaded.status()["active_sessions"], [])

    def test_orphan_recovery_preserves_run_and_refuses_replay(self):
        body = self.seed()
        sid = body["resources_session_id"]
        session = self.resources.sessions[sid]["session"]
        # Simulate a stopped worker's released handle, without recording a close.
        session.close("simulated_worker_exit")
        reloaded = backend.SyntheticResources(ResourceLedger(self.root / "ledger.json", self.root))
        reloaded.recover()
        self.assertTrue((session.run / "nemo_synthetic_orphaned.json").is_file())
        with self.assertRaises(backend.SyntheticConflict): reloaded.dispatch("/seed_session", body)


class SyntheticEvidenceAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-audit-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "evidence/mainline/unit-only/result.json"
        self.path.parent.mkdir(parents=True)
        self.journal_path = self.path.with_name("http_journal.json")
        specs = [{"task_id": "unit-" + f, "family": f, "source": "project_synthetic", "runtime_adapter": "local_files"}
                 for f in ("receivables", "meeting", "documents", "tickets")]
        registry = self.root / "tasks/registry.json"
        save(registry, {"tasks": specs})
        self.events, rows = [], []
        for spec in specs:
            sid = str(uuid.uuid4())
            episode = {"rollout_id": "unit-only-" + spec["family"]}
            task = {"taskset": "project_synthetic:diagnostic", "task_id": spec["task_id"]}
            self.events.append({"route": "/synthetic/seed_session", "status": 200,
                                "request": {"resources_session_id": sid, "episode_id": episode, "task_id": task}})
            row = {k: spec[k] for k in ("task_id", "family", "source")}
            run = self.root / "runs" / ("nemo_synthetic_" + uuid.uuid4().hex)
            save(run / "run.json", {"task_id": spec["task_id"], "workspace_owner": str(self.root)})
            for reward in (0, 1, None):
                measurement = {"source": "project_synthetic", "training_eligible": False, "reward": reward, "mask_sample": reward is None}
                if reward is not None:
                    evaluation = {"evaluation_status": "completed", "task_passed": bool(reward)}
                    target = run / ("nemo_verify_" + uuid.uuid4().hex + ".json")
                    save(target, evaluation)
                    reference = target.relative_to(self.root).as_posix()
                    measurement.update(evaluation=evaluation, evaluation_ref=reference)
                    row["positive_evaluation_ref" if reward else "negative_evaluation_ref"] = reference
                request = {"episode_id": episode, "task_id": task, "verification_input": {"resources_session_id": sid}}
                self.events.append({"route": "/synthetic/verify", "status": 200, "request": request,
                                    "response": {"episode_id": episode, "task_id": task, "result": measurement}})
            rows.append(row)
        self.receipt = {"schema_version": "nemo-synthetic-diagnostic-v1", "passed": True, "source": "project_synthetic",
                        "training_eligible": False, "real_model_calls": 0, "training_updates": 0, "admitted_training_samples": 0,
                        "task_count": 4, "expected_task_count": 4, "tasks": rows,
                        "checks": [{"name": n, "passed": True} for n in sorted(required_checks(r["task_id"] for r in rows))],
                        "source_sha256": {"tasks/registry.json": hashlib.sha256(registry.read_bytes()).hexdigest()}}
        self.write()

    def write(self):
        save(self.journal_path, self.events)
        self.receipt["http_journal_sha256"] = hashlib.sha256(self.journal_path.read_bytes()).hexdigest()
        partial = self.path.with_name("http_journal.partial.jsonl")
        raw = b"".join((json.dumps({**event, "event_index": index}, separators=(",", ":")) + "\n").encode()
                       for index, event in enumerate(self.events))
        partial.write_bytes(raw)
        checksum = hashlib.sha256(raw).hexdigest()
        tasks = [r["task_id"] for r in self.receipt["tasks"]]
        progress = self.path.with_name("progress.json")
        save(progress, {"schema_version": "scripted-diagnostic-progress-v1", "state": "passed", "passed": True,
                        "expected_tasks": tasks, "completed_tasks": tasks, "in_flight_task": None,
                        "event_count": len(self.events), "journal_bytes": len(raw), "journal_prefix_sha256": checksum,
                        "automatic_resume": False, "training_eligible": False, "source_sha256": self.receipt["source_sha256"]})
        self.receipt["incremental_journal_sha256"] = checksum
        self.receipt["progress_sha256"] = hashlib.sha256(progress.read_bytes()).hexdigest()
        save(self.path, self.receipt)

    def test_unit_evidence_counts_measured_and_unknown_separately(self):
        result = audit_synthetic(self.root, self.path)
        self.assertEqual((result["task_count"], result["valid_measurements"], result["unknown_measurements"]), (4, 8, 4))

    def test_training_claim_or_duplicate_task_is_rejected(self):
        self.receipt["training_eligible"] = True
        self.write()
        with self.assertRaises(ValueError): audit_synthetic(self.root, self.path)
        self.receipt["training_eligible"] = False
        self.receipt["tasks"][1] = self.receipt["tasks"][0]
        self.write()
        with self.assertRaises(ValueError): audit_synthetic(self.root, self.path)

    def test_unknown_numeric_or_rebound_episode_is_rejected(self):
        event = self.events[3]
        event["response"]["result"]["reward"] = 0
        self.write()
        with self.assertRaisesRegex(ValueError, "Unknown synthetic"): audit_synthetic(self.root, self.path)
        event["response"]["result"]["reward"] = None
        event["request"] = deepcopy(event["request"])
        event["request"]["verification_input"]["resources_session_id"] = str(uuid.uuid4())
        self.write()
        with self.assertRaisesRegex(ValueError, "seeded resource"): audit_synthetic(self.root, self.path)

    def test_retained_evaluator_cannot_be_borrowed_from_another_task(self):
        run = self.root / self.receipt["tasks"][0]["positive_evaluation_ref"]
        save(run.parent / "run.json", {"task_id": "another-task", "workspace_owner": str(self.root)})
        with self.assertRaisesRegex(ValueError, "another run/task"): audit_synthetic(self.root, self.path)
