"""Explicit temporary-ledger/HTTP fixtures, never evidence of a real NeMo run."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from integrations.ledger_rotation import rotate_ledger, rotation_plan
from integrations.resource_ledger import ResourceLedger
from integrations.rotation_audit import audit_rotation, fresh_tools, rotation_backend, LEDGERS


CHECKS = ("owned_bridges_idle", "all_current_identities_closed", "plan_is_read_only", "owned_bridge_stopped",
    "closed_identities_retired_in_new_generation", "archive_bytes_unchanged", "same_nemo_process", "old_sid_cannot_reseed",
    "old_close_remains_idempotent", "old_closed_session_remains_unknown", "old_episode_binding_enforced",
    "fresh_session_works", "new_generation_contains_only_fresh_identity", "original_ledger_unchanged")


class RotationAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rotation-audit-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / "evidence/mainline/ledger-rotation-unit"
        self.directory.mkdir(parents=True)
        self.receipt_path = self.directory / "result.json"

    def fixture(self, backend="isolated", *, legacy=False):
        filename, taskset, task_id = rotation_backend(backend)
        self.backend = backend
        self.path = self.root / ".runtime/nemo-vm" / filename
        episode = {"rollout_id": "explicit-unit-fixture", "attempt": 0}
        task = {"taskset": taskset, "task_id": task_id}
        self.old = {"resources_session_id": str(uuid.uuid4()), "episode_id": episode, "task_id": task, "task_data": {}}
        self.fresh = {**self.old, "resources_session_id": str(uuid.uuid4())}
        ledger = ResourceLedger(self.path, self.root)
        ledger.seed(self.old["resources_session_id"], self.old)
        ledger.update(self.old["resources_session_id"], state="closed")
        result = rotate_ledger(self.path, self.root, expected_sha256=rotation_plan(self.path, self.root)["expected_sha256"])
        ledger = ResourceLedger(self.path, self.root)
        ledger.seed(self.fresh["resources_session_id"], self.fresh)
        ledger.update(self.fresh["resources_session_id"], state="closed")
        old_bound = {k: self.old[k] for k in ("resources_session_id", "episode_id")}
        fresh_bound = {k: self.fresh[k] for k in old_bound}
        tools = ({"source": "project_synthetic", "training_eligible": False} if backend == "synthetic" else
                 {"task_profile": {"task_id": task_id, "resource_backend": "isolated", "training_admission": False},
                  "tools": [{"server": "woocommerce", "name": "woo_orders_list"}]})
        self.events = []
        def event(route, request, status=200, response=None):
            self.events.append({"route": "/"+backend+route, "request": deepcopy(request), "status": status,
                                "response": {} if response is None else deepcopy(response)})
        event("/seed_session", self.old)
        event("/close_session", old_bound)
        event("/seed_session", self.old, 409)
        event("/close_session", old_bound)
        event("/verify", {"episode_id": episode, "task_id": task, "verification_input": {"resources_session_id": self.old["resources_session_id"]}},
              response={"episode_id": episode, "task_id": task, "result": {"reward": None, "mask_sample": True}})
        event("/close_session", {**old_bound, "episode_id": {**episode, "attempt": 1}}, 409)
        event("/seed_session", self.fresh)
        event("/tools", fresh_bound, response=tools)
        if backend == "isolated":
            event("/tool", {**fresh_bound, "request_id": str(uuid.uuid4()), "server": "woocommerce", "name": "woo_orders_list",
                            "arguments": {"perPage": 1, "page": 1}}, response={"result": [{"id": 1}]})
        event("/close_session", fresh_bound)
        names = ("scripts/verify_ledger_rotation.py", "integrations/rotation_audit.py",
                 "integrations/nemo_http.py", "integrations/runtime_identity.py")
        hashes = {}
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Explicit unit source binding only\n")
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.receipt = {"schema_version": "nemo-ledger-rotation-diagnostic-v1" if legacy else "nemo-ledger-rotation-diagnostic-v2",
            "passed": True, "checks": [{"name": name, "passed": True} for name in CHECKS], "rotation": result,
            "ledger": backend, "business_task_executed": False, "official_benchmark_score_available": False,
            "execution_kind": "scripted_resource_diagnostic", "real_model_calls": 0, "training_updates": 0,
            "source_sha256": hashes, "other_ledgers": {}}
        for name in set(LEDGERS) - {filename}:
            raw = json.dumps({"schema_version": "nemo-toolathlon-resource-ledger-v1", "owner": str(self.root), "sessions": {}}).encode()
            for phase in ("before", "after"): (self.directory / (phase+"-"+name)).write_bytes(raw)
            self.receipt["other_ledgers"][name] = {p+"_sha256": hashlib.sha256(raw).hexdigest() for p in ("before", "after")}
        self.save()

    def save(self):
        raw = json.dumps(self.events).encode()
        (self.directory / "http_journal.json").write_bytes(raw)
        self.receipt["journal_sha256"] = hashlib.sha256(raw).hexdigest()
        self.receipt_path.write_text(json.dumps(self.receipt))

    def audit(self, **kwargs):
        self.save()
        return audit_rotation(self.root, self.receipt_path, **kwargs)

    def test_default_and_fixed_backend_selection(self):
        self.assertEqual(rotation_backend(), rotation_backend("synthetic"))
        self.assertEqual(rotation_backend("isolated")[0], "isolated-sessions.json")
        for value in (None, True, {}, "original", "../isolated"):
            with self.assertRaises(ValueError): rotation_backend(value)

    def test_isolated_history_and_read_tool_are_recomputed(self):
        self.fixture()
        result = self.audit(backend="isolated")
        self.assertEqual(result["archived_closed_identities"], 1)
        self.assertEqual(result["currently_retired_identities"], 1)
        self.assertFalse(result["business_task_executed"])

    def test_synthetic_v2_and_legacy_v1_remain_supported(self):
        self.fixture("synthetic")
        self.assertEqual(self.audit()["ledger"], "synthetic")
        self.receipt["schema_version"] = "nemo-ledger-rotation-diagnostic-v1"
        self.receipt.pop("ledger")
        self.assertEqual(self.audit()["ledger"], "synthetic")

    def test_wrong_backend_schema_or_ledger_path_is_rejected(self):
        self.fixture()
        with self.assertRaises(ValueError): self.audit(backend="synthetic")
        self.receipt["rotation"]["ledger_ref"] = ".runtime/nemo-vm/synthetic-sessions.json"
        with self.assertRaises(ValueError): self.audit()

    def test_cross_backend_journal_is_rejected_even_with_valid_hash(self):
        self.fixture()
        self.events[0]["route"] = "/synthetic/seed_session"
        with self.assertRaises(ValueError): self.audit()

    def test_raw_journal_changes_are_rejected(self):
        self.fixture()
        (self.directory / "http_journal.json").write_text("[]")
        with self.assertRaisesRegex(ValueError, "journal changed"): audit_rotation(self.root, self.receipt_path)

    def test_modified_or_missing_untargeted_snapshots_are_rejected(self):
        self.fixture()
        name = next(iter(self.receipt["other_ledgers"]))
        path = self.directory / ("after-"+name)
        raw = json.loads(path.read_bytes())
        raw["fixture_note"] = "changed"
        path.write_text(json.dumps(raw))
        self.receipt["other_ledgers"][name]["after_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "Another ledger"): self.audit()
        self.receipt["other_ledgers"].pop(name)
        with self.assertRaisesRegex(ValueError, "incomplete"): self.audit()

    def test_source_tampering_or_missing_required_binding_is_rejected(self):
        self.fixture()
        name = "integrations/nemo_http.py"
        original = self.receipt["source_sha256"].pop(name)
        with self.assertRaisesRegex(ValueError, "binding is incomplete"): self.audit()
        self.receipt["source_sha256"][name] = original
        (self.root/name).write_text("# changed")
        with self.assertRaisesRegex(ValueError, "Source changed"): self.audit()

    def test_archive_tampering_is_rejected(self):
        self.fixture()
        (self.root/self.receipt["rotation"]["archive_ref"]).write_text("{}")
        with self.assertRaises(ValueError): self.audit()

    def test_business_and_model_claims_are_rejected(self):
        self.fixture()
        for key, value in (("real_model_calls", True), ("training_updates", 1), ("business_task_executed", True),
                           ("official_benchmark_score_available", True)):
            previous = self.receipt[key]
            self.receipt[key] = value
            with self.assertRaises(ValueError): self.audit()
            self.receipt[key] = previous

    def test_reward_or_episode_tampering_is_rejected(self):
        self.fixture()
        self.events[4]["response"]["result"]["reward"] = 0
        with self.assertRaises(ValueError): self.audit()
        self.events[4]["response"]["result"]["reward"] = None
        self.events[4]["request"]["episode_id"]["attempt"] = 7
        with self.assertRaises(ValueError): self.audit()

    def test_missing_close_extra_write_or_reordered_operations_are_rejected(self):
        self.fixture()
        original = deepcopy(self.events)
        for events in (original[:-1], original+[deepcopy(original[-1])], original[1:]+original[:1]):
            self.events = events
            with self.assertRaises(ValueError): self.audit()

    def test_fresh_empty_orders_wrong_tool_or_foreign_binding_is_rejected(self):
        self.fixture()
        event = self.events[-2]
        for parent, key, value in ((event["response"], "result", []), (event["request"], "name", "woo_orders_update"),
                                   (event["request"], "resources_session_id", self.old["resources_session_id"])):
            original = parent[key]
            parent[key] = value
            with self.assertRaises(ValueError): self.audit()
            parent[key] = original

    def test_original_discovery_requires_fixed_task_and_method(self):
        self.assertFalse(fresh_tools("isolated", {"tools": []}))
        self.assertFalse(fresh_tools("isolated", {"task_profile": {"task_id": "wrong"},
                         "tools": [{"server": "woocommerce", "name": "woo_orders_list"}]}))
        self.assertFalse(fresh_tools("synthetic", {"source": "project_synthetic", "training_eligible": 0}))

    def test_fresh_seed_task_scope_and_close_binding_are_required(self):
        self.fixture()
        self.events[6]["request"]["task_id"]["task_id"] = "wrong"
        with self.assertRaises(ValueError): self.audit()
        self.events[6]["request"]["task_id"]["task_id"] = "wc-sales-tax-summary"
        self.events[-1]["request"]["episode_id"]["attempt"] = 9
        with self.assertRaises(ValueError): self.audit()

    def test_fresh_resource_missing_or_active_in_owned_ledger_is_rejected(self):
        self.fixture()
        raw = self.path.read_bytes()
        for state in ("active", None):
            value = json.loads(raw)
            if state is None: value["sessions"].clear()
            else: value["sessions"][self.fresh["resources_session_id"]]["state"] = state
            self.path.write_text(json.dumps(value))
            with self.assertRaises(ValueError): self.audit()

    def test_untargeted_snapshot_ownership_is_recomputed(self):
        self.fixture()
        name = next(iter(self.receipt["other_ledgers"]))
        for phase in ("before", "after"):
            path = self.directory / (phase+"-"+name)
            raw = json.loads(path.read_bytes())
            raw["owner"] = str(self.root.parent)
            path.write_text(json.dumps(raw))
            self.receipt["other_ledgers"][name][phase+"_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "ownership"): self.audit()

    def test_training_or_shared_profile_cannot_prove_isolated_tools(self):
        self.fixture()
        profile = self.events[7]["response"]["task_profile"]
        for key, value in (("resource_backend", "shared"), ("training_admission", True)):
            original = profile[key]
            profile[key] = value
            with self.assertRaises(ValueError): self.audit()
            profile[key] = original
