import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from integrations.mainline_audit import (SCHEMAS, audit_diagnostic, source_binding, initial_state_binding,
                                       collect_inputs, inputs_current, track_absence, read_json)
from integrations.mainline_audit import inventory_snapshot, track_input


class MainlineAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        source = self.root / "integrations/fixture.py"
        source.parent.mkdir()
        source.write_bytes(b"UNIT_FIXTURE = True\n")
        directory = self.root / "evidence/mainline/unit"
        directory.mkdir(parents=True)
        self.journal_path = directory / "http_journal.json"
        self.receipt_path = directory / "result.json"
        request = {"episode_id": {"rollout_id": "unit-only", "attempt": 0}, "task_id": {"taskset": "unit", "task_id": "unit"}}
        self.events = [{"route": "/verify", "status": 200, "request": request,
                        "response": {**request, "result": {"reward": None, "mask_sample": True}}}]
        self.receipt = {"schema_version": "nemo-resource-recovery-diagnostic-v1", "passed": True,
                        "checks": [{"name": name, "passed": True} for name in sorted(SCHEMAS["nemo-resource-recovery-diagnostic-v1"])],
                        "real_model_calls": 0, "training_updates": 0,
                        "source_sha256": {"integrations/fixture.py": hashlib.sha256(source.read_bytes()).hexdigest()}}
        self.write()

    def write(self):
        self.journal_path.write_text(json.dumps(self.events))
        self.receipt["journal_sha256"] = hashlib.sha256(self.journal_path.read_bytes()).hexdigest()
        self.receipt_path.write_text(json.dumps(self.receipt))

    def test_unknown_remains_unknown_in_bound_receipt(self):
        result = audit_diagnostic(self.root, self.receipt_path)
        self.assertEqual((result["valid_measurements"], result["unknown_measurements"]), (0, 1))

    def test_tampered_journal_or_source_rejected(self):
        self.journal_path.write_text("[]")
        with self.assertRaisesRegex(ValueError, "journal changed"): audit_diagnostic(self.root, self.receipt_path)
        self.write()
        (self.root / "integrations/fixture.py").write_text("CHANGED = True")
        with self.assertRaisesRegex(ValueError, "Source changed"): audit_diagnostic(self.root, self.receipt_path)

    def test_unknown_cannot_be_claimed_as_numeric_reward(self):
        self.events[0]["response"]["result"]["reward"] = 0
        self.write()
        with self.assertRaisesRegex(ValueError, "numeric task reward"): audit_diagnostic(self.root, self.receipt_path)

    def test_duplicate_named_checks_do_not_prove_complete_coverage(self):
        self.receipt["checks"][-1] = self.receipt["checks"][0]
        self.write()
        with self.assertRaisesRegex(ValueError, "required checks"): audit_diagnostic(self.root, self.receipt_path)

    def test_task_reply_binding_cannot_be_changed(self):
        self.events[0]["response"]["episode_id"] = {"rollout_id": "different-task", "attempt": 0}
        self.write()
        with self.assertRaisesRegex(ValueError, "different task"): audit_diagnostic(self.root, self.receipt_path)

    def test_workenv_evidence_helpers_are_bound_and_runtime_directories_rejected(self):
        helper = self.root / "workenv/evidence_helper.py"
        helper.parent.mkdir()
        helper.write_bytes(b"UNIT_HELPER = True\n")
        checksum = hashlib.sha256(helper.read_bytes()).hexdigest()
        self.assertTrue(source_binding(self.root, {"workenv/evidence_helper.py": checksum}))
        with self.assertRaises(ValueError): source_binding(self.root, {".runtime/private.json": checksum})

    def test_initial_snapshot_is_bound_to_its_collected_bytes(self):
        run = self.root / "runs/unit-initial-only"
        run.mkdir(parents=True)
        snapshot = run / "initial_state.json"
        snapshot.write_text(json.dumps({"tables": {}, "files": {}}), encoding="utf-8")
        metadata = {"initial_state_ref": "initial_state.json", "initial_state_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()}
        (run / "run.json").write_text(json.dumps(metadata), encoding="utf-8")
        self.assertEqual(initial_state_binding(self.root, run), metadata)
        snapshot.write_text(json.dumps({"tables": {"unit-fixture": {}}, "files": {}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "initial-state bytes changed"): initial_state_binding(self.root, run)

    def test_initial_snapshot_reference_cannot_escape_its_run(self):
        run = self.root / "runs/unit-initial-reference"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"initial_state_ref": "../another-run/initial_state.json"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "initial-state reference"): initial_state_binding(self.root, run)

    def test_input_bytes_and_absence_are_rechecked_after_an_audit(self):
        path = self.root / "evidence/mainline/unit/input.json"
        path.write_text('{"unit": 1}', encoding="utf-8")
        absent = path.with_name("stopped.pid")
        with collect_inputs(self.root) as state:
            read_json(path, self.root)
            track_absence(absent, self.root)
        receipt = {"input_sha256": state["files"], "expected_absent": sorted(state["missing"])}
        self.assertTrue(inputs_current(self.root, receipt))
        absent.write_text("unit-only", encoding="utf-8")
        self.assertFalse(inputs_current(self.root, receipt))
        absent.unlink()
        path.write_text('{"unit": 2}', encoding="utf-8")
        self.assertFalse(inputs_current(self.root, receipt))

    def test_inputs_cannot_change_between_two_reads_in_one_audit(self):
        path = self.root / "evidence/mainline/unit/input.json"
        path.write_text('{"unit": 1}', encoding="utf-8")
        with collect_inputs(self.root):
            read_json(path, self.root)
            path.write_text('{"unit": 2}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed during collection"): read_json(path, self.root)

    def test_new_program_files_invalidate_an_existing_input_inventory(self):
        from workenv.source_inventory import SOURCE_DIRS, MAINLINE_DIRS
        for name in SOURCE_DIRS + MAINLINE_DIRS: (self.root / name).mkdir(exist_ok=True)
        (self.root / "README.md").write_text("Unit fixture\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
        with collect_inputs(self.root) as state: track_input(self.root / "integrations/fixture.py", self.root)
        receipt = {"input_sha256": state["files"], "input_inventory": inventory_snapshot(self.root)}
        self.assertTrue(inputs_current(self.root, receipt))
        (self.root / "integrations/new_component.py").write_text("UNIT_ONLY = True\n", encoding="utf-8")
        self.assertFalse(inputs_current(self.root, receipt))


if __name__ == "__main__": unittest.main()
