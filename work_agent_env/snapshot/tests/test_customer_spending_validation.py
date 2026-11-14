from copy import deepcopy
from decimal import Decimal
import importlib.util
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

from adapters import customer_spending_validation as v
from adapters.office_workbook import InvalidWorkbook, read_bytes, read_tables
from training.office_task_review import public_blob, script_facts
from workenv.core import ResourceError
from adapters import meal_plan_artifacts as artifacts
from integrations.episode_cluster import EpisodeProfile, EpisodeCluster
from workenv.core import save


def sources():
    return ([{"id": 1, "first_name": "Ada", "last_name": "Li", "email": "a@example.test", "orders_count": 2, "total_spent": "10.01"},
             {"id": 2, "first_name": "Bo", "last_name": "Wu", "email": "b@example.test", "orders_count": 1, "total_spent": "10.01"},
             {"id": 3, "first_name": "Cy", "last_name": "Yu", "email": "c@example.test", "orders_count": 0, "total_spent": "0"}],
            [{"id": 1, "customer_id": 1, "total": "5.00"}, {"id": 2, "customer_id": 1, "total": "5.01"},
             {"id": 3, "customer_id": 2, "total": "10.01"}])


def outputs(customers, orders, basis=v.BASES[0]):
    expected = v.expected(customers, orders, basis=basis)
    tables = {"Customer Spending": expected["rows"], "Summary": [{"Metric": k, "Value": val} for k, val in expected["summary"].items()]}
    grid = [list(v.HEADERS["Customer Spending"]), *[list(row.values()) for row in expected["rows"]]]
    cloud = {"spreadsheets": [{"id": "own", "title": "Customer Analysis"}],
             "sheets": [{"id": 1, "spreadsheet_id": "own", "title": "Spending"}],
             "cells": [{"spreadsheet_id": "own", "sheet_id": 1, "row_index": i, "col_index": j,
                        "value": str(value), "formula": None} for i, row in enumerate(grid) for j, value in enumerate(row)]}
    return tables, cloud


class CustomerSpendingTests(unittest.TestCase):
    def setUp(self): self.customers, self.orders = sources()

    def measure(self, tables=None, cloud=None, basis=v.BASES[0]):
        original, remote = outputs(self.customers, self.orders)
        return v.measure(self.customers, self.orders, original if tables is None else tables, remote if cloud is None else cloud, basis=basis)

    def test_exact_money_count_summary_and_cloud_pass_without_task_reward(self):
        result = self.measure()
        self.assertIs(result["supplemental_passed"], True)
        self.assertIsNone(result["task_passed"])
        self.assertIsNone(result["reward"])
        truth = v.expected(self.customers, self.orders, basis=v.BASES[0])
        self.assertEqual(truth["rows"][0]["Avg_Order_Value"], Decimal("5.01"))
        self.assertEqual(truth["summary"]["Total_Spent_All"], Decimal("20.02"))

    def test_missing_basis_is_unknown_and_different_policies_are_reported(self):
        self.assertIsNone(self.measure(basis=None)["supplemental_passed"])
        self.customers[0]["total_spent"] = "99.00"
        difference = v.compare_bases(self.customers, self.orders)["different_customers"]
        self.assertEqual(len(difference), 1)
        self.assertEqual(difference[0]["customer_id"], 1)
        a = v.expected(self.customers, self.orders, basis=v.BASES[0])
        b = v.expected(self.customers, self.orders, basis=v.BASES[1])
        self.assertNotEqual(a["summary"]["Total_Spent_All"], b["summary"]["Total_Spent_All"])

    def test_source_duplicates_missing_fields_bad_numbers_and_orphans_are_unknown(self):
        for edit in (lambda: self.customers.append(deepcopy(self.customers[0])),
                     lambda: self.customers[0].pop("orders_count"),
                     lambda: self.customers[0].update(total_spent="NaN"),
                     lambda: self.orders[0].update(customer_id=99),
                     lambda: self.orders[0].update(total=True)):
            self.customers, self.orders = sources()
            edit()
            result = v.measure(self.customers, self.orders, {}, {}, basis=v.BASES[0])
            self.assertIsNone(result["supplemental_passed"])

    def test_cents_counts_identity_and_email_errors_fail(self):
        for key, value in (("Total_Spent", Decimal("10.02")), ("Orders", 3), ("Customer_ID", 4),
                           ("Email", "wrong"), ("Avg_Order_Value", "5.005")):
            tables, cloud = outputs(self.customers, self.orders)
            tables["Customer Spending"][0][key] = value
            self.assertIs(self.measure(tables, cloud)["supplemental_passed"], False)

    def test_duplicate_customer_and_summary_metric_fail(self):
        tables, cloud = outputs(self.customers, self.orders)
        tables["Customer Spending"][1] = deepcopy(tables["Customer Spending"][0])
        self.assertIs(self.measure(tables, cloud)["supplemental_passed"], False)
        tables, cloud = outputs(self.customers, self.orders)
        tables["Summary"][1] = deepcopy(tables["Summary"][0])
        self.assertIs(self.measure(tables, cloud)["supplemental_passed"], False)

    def test_ties_allow_another_top_name_but_cloud_order_must_match(self):
        tables, cloud = outputs(self.customers, self.orders)
        next(row for row in tables["Summary"] if row["Metric"] == "Top_Customer")["Value"] = "Bo Wu"
        self.assertIs(self.measure(tables, cloud)["supplemental_passed"], True)
        tables["Customer Spending"].reverse()
        self.assertIs(self.measure(tables, cloud)["supplemental_passed"], False)

    def test_unsorted_different_totals_fail(self):
        self.customers[0]["total_spent"] = "20"
        tables, cloud = outputs(self.customers, self.orders)
        tables["Customer Spending"].reverse()
        self.assertIs(self.measure(tables, cloud)["supplemental_passed"], False)

    def test_header_only_wrong_cloud_values_and_formulas_fail(self):
        for mutation in (lambda c: c.update(cells=c["cells"][:6]),
                         lambda c: c["cells"][-2].update(value="99.00"),
                         lambda c: c["cells"][-1].update(formula="=1+1"),
                         lambda c: c["cells"].append(deepcopy(c["cells"][-1])),
                         lambda c: c["cells"].pop()):
            tables, cloud = outputs(self.customers, self.orders)
            mutation(cloud)
            self.assertIs(self.measure(tables, cloud)["supplemental_passed"], False)

    def test_missing_or_ambiguous_snapshot_stays_unknown(self):
        tables, cloud = outputs(self.customers, self.orders)
        cloud["sheets"].append(deepcopy(cloud["sheets"][0]))
        self.assertIsNone(self.measure(tables, cloud)["supplemental_passed"])
        self.assertIsNone(self.measure(tables, {})["supplemental_passed"])

    def test_guest_orders_are_explicitly_excluded_and_empty_store_supported(self):
        self.orders.append({"id": 4, "customer_id": 0, "total": "99"})
        self.assertEqual(v.compare_bases(self.customers, self.orders)["guest_orders_excluded"], 1)
        truth = v.expected([], [], basis=v.BASES[1])
        self.assertEqual(truth["summary"]["Total_Active_Customers"], 0)
        self.assertEqual(truth["summary"]["Avg_Lifetime_Value"], 0)

    def test_customer_writer_rejects_paths_unknown_columns_and_nonfinite_values(self):
        tables, _ = outputs(self.customers, self.orders)
        import json
        tables = json.loads(json.dumps(tables, default=float))
        self.assertEqual(len(artifacts.validate_customer_payload(tables)), 64)
        for value in (float("nan"), True, {}, 10**400):
            changed = deepcopy(tables); changed["Customer Spending"][0]["Total_Spent"] = value
            with self.assertRaises(ValueError): artifacts.validate_customer_payload(changed)
        with self.assertRaises(ValueError): artifacts.validate_customer_payload({**tables, "path": "elsewhere.xlsx"})

    def test_extreme_source_numbers_are_unknown_without_string_conversion_overflow(self):
        for value in (10**5000, Decimal("1" * 200)):
            self.customers[0]["total_spent"] = value
            measured = v.measure(self.customers, self.orders, {}, {}, basis=v.BASES[0])
            self.assertIsNone(measured["supplemental_passed"])

    def test_complex_top_customer_is_a_business_failure(self):
        for value in ({"formula": "wrong"}, ["Ada Li"]):
            tables, cloud = outputs(self.customers, self.orders)
            next(r for r in tables["Summary"] if r["Metric"] == "Top_Customer")["Value"] = value
            self.assertIs(self.measure(tables, cloud)["supplemental_passed"], False)


@unittest.skipUnless(importlib.util.find_spec("openpyxl"), "Optional XLSX dependency")
class CustomerWorkbookTests(unittest.TestCase):
    def raw(self):
        from openpyxl import Workbook
        customers, orders = sources()
        tables, _ = outputs(customers, orders)
        wb = Workbook(); wb.remove(wb.active)
        for name, rows in tables.items():
            sheet = wb.create_sheet(name); sheet.append(v.HEADERS[name])
            for row in rows: sheet.append(list(row.values()))
        output = BytesIO(); wb.save(output); wb.close()
        return output.getvalue()

    def test_real_xlsx_roundtrip_and_bad_archive_rejected(self):
        result = read_bytes(self.raw(), v.HEADERS)
        self.assertEqual(len(result["Customer Spending"]), 2)
        with self.assertRaises(InvalidWorkbook): read_bytes(b"not zip", v.HEADERS)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError): read_tables(temp, "../outside.xlsx", v.HEADERS)

    def test_hidden_dimension_cannot_hide_wrong_customer(self):
        source, target = BytesIO(self.raw()), BytesIO()
        with ZipFile(source) as original, ZipFile(target, "w", ZIP_DEFLATED) as changed:
            for member in original.infolist():
                raw = original.read(member)
                if member.filename == "xl/worksheets/sheet1.xml":
                    raw = raw.replace(b'ref="A1:F3"', b'ref="A1:F1"')
                changed.writestr(member.filename, raw)
        result = read_bytes(target.getvalue(), v.HEADERS)
        self.assertEqual(len(result["Customer Spending"]), 2)

    def episode(self, root):
        import json
        profile = EpisodeProfile(root, "9d8c116b-0f7c-4c88-b690-4183972c37cd", 0)
        workspace = profile.base / "customer-report-workspace"
        workspace.mkdir(parents=True)
        save(profile.base / "cluster_owner.json", profile.record())
        save(profile.base / "customer-report-initialized.json", {"state": "ready", "task_id": v.TASK_ID, "profile": profile.record()})
        cluster = EpisodeCluster(profile); cluster.started = True
        tables, _ = outputs(*sources())
        return cluster, workspace, json.loads(json.dumps(tables, default=float))

    def test_customer_transaction_roundtrip_and_task_marker_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cluster, workspace, tables = self.episode(root)
            tables["Customer Spending"][0]["Name"] = "=literal"
            artifacts.write_report(root, cluster, tables, task="customer_report")
            read = read_tables(workspace, "WC_Customer_Report.xlsx", v.HEADERS)
            self.assertEqual(read["Customer Spending"][0]["Name"], "=literal")
            with self.assertRaises(FileNotFoundError): artifacts.workspace_for(root, cluster, task="order_analysis")
            marker = cluster.profile.base / "customer-report-initialized.json"
            save(marker, {"state": "ready", "task_id": "another-task", "profile": cluster.profile.record()})
            with self.assertRaises(ResourceError): artifacts.read_report(root, cluster, task="customer_report")

    def test_customer_interrupted_transaction_preserves_bytes_and_reconciles_without_replay(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cluster, workspace, tables = self.episode(root)
            artifacts.write_report(root, cluster, tables, task="customer_report")
            original = (workspace / "WC_Customer_Report.xlsx").read_bytes()
            tables["Customer Spending"][0]["Name"] = "changed"
            with patch.object(artifacts, "replace_atomic", side_effect=PermissionError("fixture")):
                with self.assertRaises(PermissionError): artifacts.write_report(root, cluster, tables, task="customer_report")
            self.assertEqual((workspace / "WC_Customer_Report.xlsx").read_bytes(), original)
            with self.assertRaises(ResourceError): artifacts.require_no_pending_writes(cluster, task="customer_report")
            intent = next(p for p in cluster.profile.base.glob("customer-report-artifact-write-*.json") if json.loads(p.read_text())["state"] == "prepared")
            result = artifacts.reconcile_write(root, cluster, intent.name, task="customer_report")
            self.assertEqual(result["decision"], "not_committed")
            self.assertIs(result["replay_performed"], False)


class OfficeReviewTests(unittest.TestCase):
    def test_reference_or_unregistered_path_never_reaches_git(self):
        with patch("training.office_task_review.pinned_blob") as read:
            for path in ("groundtruth_workspace/report.xlsx", "solution/main.py", "../evaluation/main.py"):
                with self.assertRaises(ValueError): public_blob(Path("unused"), v.TASK_ID, path)
            read.assert_not_called()

    def test_reset_scope_and_ast_reference_detection_without_execution(self):
        reset = "\n".join("cur.execute('DELETE FROM gsheet."+table+"')" for table in ("cells", "sheets", "permissions", "spreadsheets", "folders"))
        facts = script_facts(reset, "gt_file = 'not read'\nraise RuntimeError('must not execute')")
        self.assertIs(facts["evaluator_mentions_groundtruth_file"], True)
        with self.assertRaises(ValueError): script_facts(reset.replace("gsheet.cells", "wc.orders"), "pass")
