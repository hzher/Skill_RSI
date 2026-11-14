from copy import deepcopy
from decimal import Decimal
import importlib.util
from io import BytesIO
from pathlib import Path
import re
import tempfile
import unittest
from zipfile import ZipFile, ZIP_DEFLATED

from adapters import fulfillment_validation as v


class FulfillmentContractTests(unittest.TestCase):
    def setUp(self):
        self.orders = [{"id": i + 1, "status": status, "total": "10.005" if i == 0 else "10.00"}
                       for i, status in enumerate(("completed", "processing", "pending", "on-hold", "cancelled", "failed", "refunded"))]
        self.binding = v.source_identity()
        rows, summary = v.expected(self.orders, source_binding=self.binding)
        self.tables = {"Order Status": rows, "Fulfillment Summary": [{"Metric": k, "Value": value} for k, value in summary.items()]}
        self.cloud = deepcopy(rows)

    def measure(self): return v.measure_rows(self.orders, self.tables, self.cloud, source_binding=self.binding)

    def test_all_statuses_counts_exact_money_and_percentages(self):
        measured = self.measure()
        self.assertIs(measured["supplemental_passed"], True)
        self.assertIsNone(measured["reward"])
        rows, summary = v.expected(self.orders, source_binding=self.binding)
        self.assertEqual(next(r for r in rows if r["Status"] == "completed")["Total_Revenue"], Decimal("10.01"))
        self.assertEqual(summary["Fulfillment_Rate"], Decimal("14.29"))
        self.assertEqual(summary["Pending_Processing_Count"], 3)
        self.assertEqual(summary["Cancellation_Count"], 2)
        self.assertEqual(summary["Cancellation_Rate"], Decimal("28.57"))

    def test_missing_status_and_wrong_status_order_are_business_failures(self):
        self.tables["Order Status"].pop()
        self.assertIs(self.measure()["supplemental_passed"], False)
        self.setUp(); self.tables["Order Status"].reverse()
        self.assertIs(self.measure()["supplemental_passed"], False)

    def test_cent_precision_and_duplicate_status_rejected(self):
        self.tables["Order Status"][0]["Total_Revenue"] += Decimal("0.01")
        self.assertIs(self.measure()["supplemental_passed"], False)
        self.setUp(); self.tables["Order Status"].append(deepcopy(self.tables["Order Status"][0]))
        self.assertIs(self.measure()["supplemental_passed"], False)

    def test_refunded_is_not_added_to_cancellation_or_pending(self):
        for row in self.tables["Fulfillment Summary"]:
            if row["Metric"] == "Cancellation_Count": row["Value"] = 3
        self.assertIs(self.measure()["supplemental_passed"], False)

    def test_duplicate_or_unknown_summary_metrics_rejected(self):
        self.tables["Fulfillment Summary"][-1] = deepcopy(self.tables["Fulfillment Summary"][0])
        self.assertIs(self.measure()["supplemental_passed"], False)
        self.setUp(); self.tables["Fulfillment Summary"][-1]["Metric"] = "invented"
        self.assertIs(self.measure()["supplemental_passed"], False)

    def test_cloud_data_must_equal_local_data(self):
        self.cloud[0]["Order_Count"] = 123
        self.assertIs(self.measure()["supplemental_passed"], False)
        self.cloud = None
        self.assertIsNone(self.measure()["supplemental_passed"])

    def test_duplicate_source_id_missing_total_nonfinite_or_uppercase_unknown(self):
        for change in (lambda: self.orders.append(deepcopy(self.orders[0])), lambda: self.orders[0].pop("total"),
                       lambda: self.orders[0].update(total="NaN"), lambda: self.orders[0].update(status="Completed")):
            self.setUp(); change()
            self.assertIsNone(self.measure()["supplemental_passed"])

    def test_foreign_public_source_binding_stays_unknown(self):
        self.binding["task_id"] = "wc-customer-spending-report"
        self.assertIsNone(self.measure()["supplemental_passed"])

    def test_empty_fixture_is_defined_without_claiming_task_success(self):
        rows, summary = v.expected([], source_binding=self.binding)
        tables = {"Order Status": rows, "Fulfillment Summary": [{"Metric": k, "Value": value} for k, value in summary.items()]}
        measured = v.measure_rows([], tables, [], source_binding=self.binding)
        self.assertIs(measured["supplemental_passed"], True)
        self.assertIsNone(measured["task_passed"])
        self.assertEqual(summary["Fulfillment_Rate"], 0)
        self.assertEqual(summary["Cancellation_Rate"], 0)


@unittest.skipUnless(importlib.util.find_spec("openpyxl"), "Optional XLSX dependency")
class FulfillmentWorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.orders = [{"id": 1, "status": "completed", "total": "7.00"},
                       {"id": 2, "status": "failed", "total": "3.00"}]
        rows, summary = v.expected(self.orders, source_binding=v.source_identity())
        self.cloud = deepcopy(rows)
        self.tables = {"Order Status": rows, "Fulfillment Summary": [{"Metric": k, "Value": value} for k, value in summary.items()]}

    def write(self):
        from openpyxl import Workbook
        workbook = Workbook(); workbook.remove(workbook.active)
        for name, rows in self.tables.items():
            sheet = workbook.create_sheet(name); sheet.append(v.HEADERS[name])
            for row in rows: sheet.append([row[k] for k in v.HEADERS[name]])
        workbook.save(self.root / "Fulfillment_Report.xlsx"); workbook.close()

    def measure(self, binding=None):
        return v.measure_workbook(self.orders, self.root, self.cloud, source_binding=v.source_identity() if binding is None else binding)

    def test_real_workbook_and_cloud_rows_pass_without_runtime_claim(self):
        self.write()
        measured = self.measure()
        self.assertIs(measured["supplemental_passed"], True)
        self.assertIs(measured["nemo_task_executed"], False)
        self.assertIsNone(measured["reward"])

    def test_missing_workbook_fails_but_missing_source_stays_unknown(self):
        self.assertIs(self.measure()["supplemental_passed"], False)
        self.assertIsNone(self.measure({})["supplemental_passed"])

    def test_formula_and_wrong_column_are_rejected(self):
        self.tables["Order Status"][0]["Total_Revenue"] = "=7+1"
        self.write()
        self.assertIs(self.measure()["supplemental_passed"], False)
        from openpyxl import load_workbook
        workbook = load_workbook(self.root / "Fulfillment_Report.xlsx")
        workbook["Order Status"]["A1"] = "wrong header"
        workbook.save(self.root / "Fulfillment_Report.xlsx"); workbook.close()
        self.assertIs(self.measure()["supplemental_passed"], False)

    def test_forged_dimensions_do_not_hide_wrong_later_status(self):
        self.tables["Order Status"][-1]["Status"] = "invented-status"
        self.write()
        file = self.root / "Fulfillment_Report.xlsx"
        target = BytesIO()
        with ZipFile(BytesIO(file.read_bytes())) as source, ZipFile(target, "w", ZIP_DEFLATED) as changed:
            for member in source.infolist():
                raw = source.read(member)
                if member.filename == "xl/worksheets/sheet1.xml": raw = re.sub(b'<dimension ref="[^"]+"', b'<dimension ref="A1:D1"', raw)
                changed.writestr(member.filename, raw)
        file.write_bytes(target.getvalue())
        self.assertIs(self.measure()["supplemental_passed"], False)
