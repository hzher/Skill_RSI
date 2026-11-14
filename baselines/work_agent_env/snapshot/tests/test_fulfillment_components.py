"""Failure boundaries that must hold before a new office task is activated."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adapters import fulfillment_validation as validation
from adapters import meal_plan_artifacts as artifacts
from adapters.fulfillment_evaluation import classify_original, measure_snapshot
from adapters.customer_spending_validation import cloud_rows, InvalidReport
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile
from workenv.core import ResourceError, save


class FulfillmentBoundaryTests(unittest.TestCase):
    def test_database_exception_in_original_fail_is_unknown(self):
        self.assertIsNone(classify_original(1, "Google Sheet check error: connection refused\n=== RESULT: FAIL (1 errors) ===", ""))
        self.assertIs(classify_original(0, "=== RESULT: PASS ===", ""), True)
        self.assertIs(classify_original(1, "=== RESULT: FAIL (2 errors) ===", ""), False)
        self.assertIsNone(classify_original(0, "=== RESULT: PASS ===\n=== RESULT: FAIL (1 errors) ===", ""))
        self.assertIsNone(classify_original(0, "=== RESULT: PASS ===", "Traceback"))

    def cloud(self):
        columns = validation.HEADERS["Order Status"]
        return {"spreadsheets": [{"id": "own", "title": "Order Fulfillment Dashboard"}],
                "sheets": [{"id": "tab", "spreadsheet_id": "own", "title": "Status Breakdown"}],
                "cells": [{"spreadsheet_id": "own", "sheet_id": "tab", "row_index": r, "col_index": c, "value": v}
                          for r, values in enumerate((columns, ("completed", "2", "20.00", "10.00"))) for c, v in enumerate(values)]}

    def test_cloud_exact_title_tab_parent_and_unique_identity(self):
        data = self.cloud()
        self.assertEqual(cloud_rows(data, layout="fulfillment")[0]["Status"], "completed")
        data["sheets"][0]["spreadsheet_id"] = "foreign"
        with self.assertRaises(InvalidReport): cloud_rows(data, layout="fulfillment")
        data = self.cloud(); data["sheets"].append(deepcopy(data["sheets"][0]))
        with self.assertRaises(ResourceError): cloud_rows(data, layout="fulfillment")
        data = self.cloud(); data["spreadsheets"][0]["title"] = "order approximate"
        with self.assertRaises(InvalidReport): cloud_rows(data, layout="fulfillment")

    def test_cloud_formula_extra_column_and_missing_cell_rejected(self):
        for mutate in (lambda d: d["cells"][4].update(formula="=1+1"),
                       lambda d: d["cells"].append({**d["cells"][-1], "col_index": 4}),
                       lambda d: d["cells"].pop()):
            data = self.cloud(); mutate(data)
            with self.assertRaises(InvalidReport): cloud_rows(data, layout="fulfillment")


@unittest.skipUnless(importlib.util.find_spec("openpyxl"), "Optional XLSX dependency")
class FulfillmentTransactionTests(unittest.TestCase):
    def episode(self, root):
        profile = EpisodeProfile(root, "9d8c116b-0f7c-4c88-b690-4183972c37cd", 0)
        workspace = profile.base / "fulfillment-workspace"
        workspace.mkdir(parents=True)
        save(profile.base / "cluster_owner.json", profile.record())
        save(profile.base / "fulfillment-initialized.json", {"state": "ready", "task_id": validation.TASK_ID, "profile": profile.record()})
        cluster = EpisodeCluster(profile); cluster.started = True
        orders = [{"id": 1, "status": "completed", "total": "10.00"}]
        rows, summary = validation.expected(orders, source_binding=validation.source_identity())
        tables = {"Order Status": rows, "Fulfillment Summary": [{"Metric": k, "Value": v} for k, v in summary.items()]}
        return cluster, workspace, orders, json.loads(json.dumps(tables, default=float))

    def test_transaction_history_and_foreign_task_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cluster, workspace, orders, tables = self.episode(root)
            original = artifacts.write_report(root, cluster, tables, task="fulfillment")
            tables["Order Status"][0]["Total_Revenue"] += .01
            changed = artifacts.write_report(root, cluster, tables, task="fulfillment")
            self.assertEqual(changed["previous_sha256"], original["sha256"])
            archive = cluster.profile.base / "fulfillment-artifact-history" / (original["sha256"] + ".xlsx")
            self.assertTrue(archive.is_file())
            save(cluster.profile.base / "fulfillment-initialized.json", {"state": "ready", "task_id": "wc-customer-spending-report", "profile": cluster.profile.record()})
            with self.assertRaises(ResourceError): artifacts.read_report(root, cluster, task="fulfillment")
            with self.assertRaises(FileNotFoundError): artifacts.workspace_for(root, cluster, task="customer_report")

    def test_interrupted_replace_retains_old_bytes_and_blocks_until_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cluster, workspace, orders, tables = self.episode(root)
            artifacts.write_report(root, cluster, tables, task="fulfillment")
            original = (workspace / "Fulfillment_Report.xlsx").read_bytes()
            tables["Order Status"][0]["Order_Count"] = 7
            with patch.object(artifacts, "replace_atomic", side_effect=PermissionError("fixture")):
                with self.assertRaises(PermissionError): artifacts.write_report(root, cluster, tables, task="fulfillment")
            self.assertEqual((workspace / "Fulfillment_Report.xlsx").read_bytes(), original)
            with self.assertRaises(ResourceError): artifacts.require_no_pending_writes(cluster, task="fulfillment")
            intent = next(p for p in cluster.profile.base.glob("fulfillment-artifact-write-*.json") if json.loads(p.read_text())["state"] == "prepared")
            result = artifacts.reconcile_write(root, cluster, intent.name, task="fulfillment")
            self.assertEqual(result["decision"], "not_committed")
            self.assertIs(result["replay_performed"], False)

    def test_missing_cloud_is_business_failure_and_missing_snapshot_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cluster, workspace, orders, tables = self.episode(root)
            artifacts.write_report(root, cluster, tables, task="fulfillment")
            data = {"orders": orders, "spreadsheets": [], "sheets": [], "cells": []}
            self.assertIs(measure_snapshot(data, workspace)["supplemental_passed"], False)
            data.pop("cells")
            with self.assertRaises(ResourceError): measure_snapshot(data, workspace)
