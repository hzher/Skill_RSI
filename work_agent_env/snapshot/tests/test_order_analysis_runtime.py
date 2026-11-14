"""Task-limited order workbook transactions and original evaluator classification."""
import base64
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adapters import meal_plan_artifacts as artifacts
from adapters.order_analysis_evaluation import classify_original, original_checks
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile
from workenv.core import ResourceError, save


def payload():
    return {"Status_Summary": [{"Status": "=literal", "Order_Count": 3, "Total_Revenue": 9.5, "Avg_Order_Value": 3.17}],
            "Low_Stock_Products": [{"Product_Name": "=literal", "SKU": None, "Stock_Quantity": 2, "Total_Sales": 5}]}


class OrderArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = EpisodeProfile(self.root, "a4489a8e-aed3-41ec-9d8b-3da40d90bc65", 0)
        self.workspace = self.profile.base / "order-analysis-workspace"
        self.workspace.mkdir(parents=True)
        save(self.profile.base / "cluster_owner.json", self.profile.record())
        save(self.profile.base / "order-analysis-initialized.json", {"state": "ready", "task_id": "wc-order-analysis-gcal", "profile": self.profile.record()})
        self.cluster = EpisodeCluster(self.profile)
        self.cluster.started = True

    def test_order_tables_reject_path_formula_objects_nonfinite_and_extra_columns(self):
        self.assertEqual(len(artifacts.validate_order_payload(payload())), 64)
        with self.assertRaises(ValueError): artifacts.validate_order_payload({**payload(), "path": "x.xlsx"})
        for value in ({"formula": "SUM(A1)"}, float("nan"), True, 10**400):
            data = payload()
            data["Status_Summary"][0]["Total_Revenue"] = value
            with self.assertRaises(ValueError): artifacts.validate_order_payload(data)
        data = payload()
        data["Low_Stock_Products"][0]["unexpected"] = 1
        with self.assertRaises(ValueError): artifacts.validate_order_payload(data)

    def test_meal_and_order_initialization_cannot_be_substituted(self):
        with self.assertRaises(FileNotFoundError): artifacts.workspace_for(self.root, self.cluster)
        with self.assertRaises(ValueError): artifacts.artifact_layout("arbitrary-path")
        with self.assertRaises(ValueError): artifacts.workspace_for(self.root / "foreign", self.cluster, task="order_analysis")

    @unittest.skipUnless(importlib.util.find_spec("openpyxl"), "Optional workbook dependency unavailable")
    def test_roundtrip_keeps_exact_headers_literals_history_and_old_task_names_separate(self):
        from openpyxl import load_workbook
        first = artifacts.write_report(self.root, self.cluster, payload(), task="order_analysis")
        downloaded = artifacts.read_report(self.root, self.cluster, task="order_analysis")
        raw = base64.b64decode(downloaded["base64"])
        wb = load_workbook(self.workspace / "Order_Analysis.xlsx")
        self.assertEqual([c.value for c in wb["Low_Stock_Products"][1]], ["Product_Name", "SKU", "Stock_Quantity", "Total_Sales"])
        self.assertEqual(wb["Status_Summary"]["A2"].data_type, "s")
        wb.close()
        data = payload(); data["Status_Summary"][0]["Total_Revenue"] = 11
        second = artifacts.write_report(self.root, self.cluster, data, task="order_analysis")
        self.assertEqual(first["sha256"], second["previous_sha256"])
        self.assertEqual((self.profile.base / "order-analysis-artifact-history" / (first["sha256"] + ".xlsx")).read_bytes(), raw)
        self.assertFalse((self.profile.base / "calendar-workspace").exists())

    @unittest.skipUnless(importlib.util.find_spec("openpyxl"), "Optional workbook dependency unavailable")
    def test_interrupted_replace_is_not_replayed_and_can_be_reconciled(self):
        artifacts.write_report(self.root, self.cluster, payload(), task="order_analysis")
        changed = payload()
        changed["Status_Summary"][0]["Total_Revenue"] = 123
        with patch.object(artifacts, "replace_atomic", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError): artifacts.write_report(self.root, self.cluster, changed, task="order_analysis")
        with self.assertRaises(ResourceError): artifacts.require_no_pending_writes(self.cluster, task="order_analysis")
        intent = next(p for p in self.profile.base.glob("order-analysis-artifact-write-*.json") if json.loads(p.read_text())["state"] == "prepared")
        result = artifacts.reconcile_write(self.root, self.cluster, intent.name, task="order_analysis")
        self.assertEqual(result["decision"], "not_committed")
        self.assertFalse(result["replay_performed"])
        (self.profile.base / "calendar-workspace").mkdir()
        save(self.profile.base / "calendar-initialized.json", {"state": "ready", "profile": self.profile.record()})
        with self.assertRaises(ValueError): artifacts.reconcile_write(self.root, self.cluster, intent.name)


class OrderOriginalClassifierTests(unittest.TestCase):
    def test_fixed_original_checks_never_expose_details_or_dynamic_business_identifiers(self):
        raw=("  [FAIL] Event description references low-stock context: PRIVATE_DESCRIPTION\n"
             "  [PASS] Status_Summary row count\n"
             "  [FAIL] SKU 'PRIVATE_SKU' Stock_Quantity: Expected PRIVATE_NUMBER\n"
             "  [FAIL] Event description references low-stock context PRIVATE_VALUE: ignored\n")
        checks=original_checks(raw)
        self.assertEqual(checks,[{'name':'calendar_description_low_stock_context','passed':False},
                                 {'name':'status_row_count','passed':True}])
        self.assertNotIn('PRIVATE',str(checks))

    def test_conflicting_repeated_original_checks_keep_failure_and_remain_bounded(self):
        raw=("[PASS] Event description is not empty\n"
             "[FAIL] Event description is not empty: private\n"
             "[PASS] Event description is not empty\n")
        self.assertEqual(original_checks(raw),[{'name':'calendar_description_present','passed':False}])
        self.assertEqual(original_checks('x'*(256*1024+1)),[])
        self.assertEqual(original_checks(None),[])

    def test_original_failure_category_reaches_model_without_changing_original_score(self):
        from integrations.model_task_executor import measurement_view
        raw=("INFO: Using dynamically computed expected values from PostgreSQL\n"
             "[FAIL] Event description references low-stock context: PRIVATE_DESCRIPTION\nOverall: FAIL\n")
        upstream=classify_original(1,raw,'')
        view=measurement_view({'reward':0,'evaluation':{'upstream_passed':upstream,
            'supplemental_passed':True,'upstream_checks':original_checks(raw)}})
        self.assertIs(view['upstream_passed'],False)
        self.assertEqual(view['failed_checks'],['calendar_description_low_stock_context'])
        self.assertNotIn('PRIVATE',str(view))

    def test_original_business_pass_and_failure_require_database_path_and_matching_exit(self):
        prefix = "INFO: Using dynamically computed expected values from PostgreSQL\n"
        self.assertIs(classify_original(0, prefix+"Overall: PASS\n", ""), True)
        self.assertIs(classify_original(1, prefix+"Overall: FAIL\n", ""), False)
        self.assertIsNone(classify_original(1, prefix+"Overall: PASS\n", ""))

    def test_fallback_database_warning_traceback_and_duplicate_summary_are_unknown(self):
        for stdout, stderr in (("INFO: Falling back to groundtruth Excel\nOverall: FAIL", ""),
            ("WARNING: Could not connect to PostgreSQL\nOverall: FAIL", ""),
            ("INFO: Using dynamically computed expected values from PostgreSQL\nOverall: PASS\nOverall: PASS", ""),
            ("INFO: Using dynamically computed expected values from PostgreSQL\nOverall: PASS", "Traceback")):
            self.assertIsNone(classify_original(0, stdout, stderr))
