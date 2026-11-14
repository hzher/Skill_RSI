import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from integrations.mainline_audit import inventory_snapshot
from integrations.task_profiles import MEAL_IMPLEMENTATION_FILES, build_registry
from training.runtime_capabilities import (
    AUDIT, COMMON_CHECKS, CONTRACTS, DECLARATIONS, EXECUTION_CHECKS,
    LIFECYCLE_CHECKS, PROFILE_PATH, PROJECTION_SOURCE, SOURCE_PIN, project_capabilities,
)
from workenv.source_inventory import MAINLINE_DIRS, SOURCE_DIRS, source_files


class RuntimeCapabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in (*SOURCE_DIRS, *MAINLINE_DIRS):
            (self.root / name).mkdir(exist_ok=True)
        for name in ("README.md", ".gitignore", PROJECTION_SOURCE,
                     "scripts/audit_mainline.py", "integrations/mainline_audit.py"):
            (self.root / name).write_text("# Unit fixture only\n", encoding="utf-8")
        self.write(SOURCE_PIN, {"sources": {"toolathlon_gym": {"commit": "a" * 40}}})
        self.tax, self.meal = "wc-sales-tax-summary", "howtocook-meal-plan-gcal"
        self.declarations = {
            self.tax: self.declaration(["woocommerce", "google_sheet"], legacy=True),
            self.meal: self.declaration(["howtocook", "google_calendar"]),
            "unadapted-task": self.declaration(["howtocook", "google_calendar"], legacy=True),
        }
        self.write_declarations()
        fixtures = {
            "adapters/toolathlon_native.py": 'TASK_ID="wc-sales-tax-summary"\nraise RuntimeError("do not import")\n',
            "adapters/woocommerce_native.mjs": "// Static fixture only\n",
            "integrations/toolathlon_bridge.py": 'ALLOWED={"woocommerce":{"unit_read"},"google_sheet":{"unit_list"}}\n',
        }
        for name in MEAL_IMPLEMENTATION_FILES:
            fixtures[name] = '# Static fixture only\n'
        fixtures["integrations/meal_plan_services.py"] = (
            'TASK_ID="howtocook-meal-plan-gcal"\n'
            'METHODS={"howtocook":("mcp_howtocook_getAllRecipes","mcp_howtocook_getRecipeById"),'
            '"google_calendar":("create_event","get_event","update_event","delete_event","list_events")}\n'
            'raise RuntimeError("do not import")\n')
        for name, content in fixtures.items():
            (self.root / name).write_text(content, encoding="utf-8")
        self.write(PROFILE_PATH, build_registry(self.root))
        self.refresh_audit()

    def declaration(self, services, *, legacy=False):
        return {"configuration_status": "inspected", "config_git_blob": "b" * 40,
            "config_sha256": "c" * 64, "needed_mcp_servers": services, "needed_local_tools": [],
            "complete_task_adapter_available": legacy, "private_canary": "DO_NOT_EXPORT"}

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def read(self, name):
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def checksum(self, name):
        return hashlib.sha256((self.root / name).read_bytes()).hexdigest()

    def write_declarations(self):
        self.write(DECLARATIONS, {"schema_version": "toolathlon-seed-capabilities-v1",
            "source": "toolathlon_gym", "commit": "a" * 40, "tasks": self.declarations})

    def refresh_audit(self):
        sources = {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in source_files(self.root, include_mainline=True)
                   if p.suffix in (".py", ".mjs", ".json")}
        self.write(CONTRACTS, {"passed": True, "source_sha256": sources})
        names = set(COMMON_CHECKS)
        for group in (*EXECUTION_CHECKS.values(), *LIFECYCLE_CHECKS.values()):
            names.update(group)
        self.write(AUDIT, {"schema_version": "mainline-consistency-audit-v1", "passed": True,
            "checks": [{"name": name, "passed": True} for name in sorted(names)],
            "contracts_sha256": self.checksum(CONTRACTS),
            "source_sha256": {"scripts/audit_mainline.py": self.checksum("scripts/audit_mainline.py")},
            "input_sha256": {**sources, CONTRACTS: self.checksum(CONTRACTS)},
            "expected_absent": [], "input_inventory": inventory_snapshot(self.root)})

    def assert_unverified(self):
        report = project_capabilities(self.root)
        self.assertFalse(report["audit"]["current"])
        self.assertEqual(report["summary"]["tasks_with_current_execution_evidence"], [])
        self.assertEqual(report["summary"]["current_execution_backend_contracts"], 0)
        return report

    def test_two_audited_tasks_three_backends_do_not_admit_other_declared_tasks(self):
        before = {name: (self.root / name).read_bytes() for name in (DECLARATIONS, PROFILE_PATH, AUDIT)}
        report = project_capabilities(self.root)
        self.assertTrue(report["audit"]["current"])
        self.assertEqual(report["summary"]["tasks_with_current_execution_evidence"], [self.meal, self.tax])
        self.assertEqual(report["summary"]["current_execution_backend_contracts"], 3)
        self.assertEqual(report["summary"]["tasks_without_current_execution_evidence"], 1)
        self.assertEqual(report["tasks"]["unadapted-task"]["observed_runtime_backends"], {})
        self.assertEqual(set(report["tasks"][self.meal]["observed_runtime_backends"]), {"isolated"})
        self.assertEqual(report["automatic_training_admissions"], 0)
        self.assertFalse(report["training_eligibility_evaluated"])
        self.assertFalse(report["live_service_health_checked"])
        self.assertNotIn("DO_NOT_EXPORT", json.dumps(report))
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})

    def test_legacy_adapter_flags_neither_enable_nor_disable_audited_execution(self):
        report = project_capabilities(self.root)
        self.assertFalse(report["declaration_inventory"]["legacy_adapter_annotations_used_as_runtime_evidence"])
        self.assertIn(self.meal, report["summary"]["tasks_with_current_execution_evidence"])
        self.assertNotIn("unadapted-task", report["summary"]["tasks_with_current_execution_evidence"])

    def test_missing_failed_or_numeric_pass_flags_never_reuse_old_success(self):
        original = self.read(AUDIT)
        for value in (False, 1, "true", None):
            with self.subTest(value=value):
                self.write(AUDIT, {**original, "passed": value})
                self.assert_unverified()
        (self.root / AUDIT).unlink()
        self.assert_unverified()

    def test_failed_duplicate_or_missing_check_downgrades_evidence(self):
        for mode in ("failed", "duplicate", "missing_common"):
            with self.subTest(mode=mode):
                self.refresh_audit()
                audit = self.read(AUDIT)
                if mode == "failed":
                    audit["checks"][0]["passed"] = False
                elif mode == "duplicate":
                    audit["checks"].append(audit["checks"][0])
                else:
                    audit["checks"] = [c for c in audit["checks"] if c["name"] != COMMON_CHECKS[0]]
                self.write(AUDIT, audit)
                self.assert_unverified()

    def test_component_proofs_cannot_replace_real_nemo_execution(self):
        audit = self.read(AUDIT)
        audit["checks"] = [c for c in audit["checks"] if c["name"] != "actual_nemo_original_meal_plan"]
        audit["checks"].extend({"name": name, "passed": True} for name in
            ("original_meal_plan_local_evaluators", "original_meal_plan_private_workers"))
        self.write(AUDIT, audit)
        report = project_capabilities(self.root)
        self.assertEqual(report["summary"]["tasks_with_current_execution_evidence"], [self.tax])
        self.assertEqual(report["tasks"][self.meal]["observed_runtime_backends"]["isolated"]["recovery_and_expiry"], "unverified")

    def test_lifecycle_and_execution_are_separate_claims(self):
        audit = self.read(AUDIT)
        audit["checks"] = [c for c in audit["checks"] if c["name"] != "actual_nemo_meal_lifecycle"]
        self.write(AUDIT, audit)
        report = project_capabilities(self.root)
        self.assertEqual(report["summary"]["tasks_with_current_execution_evidence"], [self.meal, self.tax])
        self.assertEqual(report["summary"]["tasks_with_current_lifecycle_evidence"], [self.tax])

    def test_changed_code_and_new_receipt_inventory_invalidate_audit(self):
        path = self.root / "adapters/toolathlon_native.py"
        path.write_text(path.read_text(encoding="utf-8") + "# Changed fixture\n", encoding="utf-8")
        report = self.assert_unverified()
        self.assertEqual(report["profile_registry"]["status"], "stale")
        self.write(PROFILE_PATH, build_registry(self.root))
        self.refresh_audit()
        self.write("evidence/mainline/new-failed-result/result.json", {"passed": False})
        self.assert_unverified()

    def test_changed_contracts_projection_source_or_expected_absence_invalidate_audit(self):
        for name in (CONTRACTS, PROJECTION_SOURCE):
            with self.subTest(name=name):
                self.refresh_audit()
                path = self.root / name
                path.write_bytes(path.read_bytes() + b"\n")
                self.assert_unverified()
        self.refresh_audit()
        audit = self.read(AUDIT)
        audit["expected_absent"] = ["evidence/mainline/owned-worker.pid"]
        self.write(AUDIT, audit)
        self.write("evidence/mainline/owned-worker.pid", {"unit": True})
        self.assert_unverified()

    def test_missing_freshness_inventory_or_declaration_binding_is_not_current(self):
        for key in ("inventory", "metadata_binding"):
            with self.subTest(key=key):
                self.refresh_audit()
                audit = self.read(AUDIT)
                if key == "inventory":
                    audit.pop("input_inventory")
                else:
                    audit["input_sha256"].pop(DECLARATIONS)
                self.write(AUDIT, audit)
                self.assert_unverified()

    def test_source_pin_mismatch_and_bad_public_metadata_are_rejected(self):
        declaration = self.read(DECLARATIONS)
        self.write(DECLARATIONS, {**declaration, "commit": "d" * 40})
        with self.assertRaisesRegex(ValueError, "source pin"):
            project_capabilities(self.root)
        self.write_declarations()
        self.declarations[self.tax]["needed_mcp_servers"] = ["https://private.invalid/task"]
        self.write_declarations()
        with self.assertRaisesRegex(ValueError, "service/tool"):
            project_capabilities(self.root)


if __name__ == "__main__":
    unittest.main()
