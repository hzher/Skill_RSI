"""Supplied synthetic snapshots only; no database or task answer artifacts."""
from copy import deepcopy
from decimal import Decimal, Inexact, localcontext
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

from adapters import order_analysis_validation as analysis
from workenv.core import ResourceError


def fixture():
    orders = [{"status": "completed", "total": "10.005", "date_created": "1990-01-01"},
        {"status": "completed", "total": "0.005", "date_created": "2099-01-01"},
        {"status": "refunded", "total": "-1.005"}, {"status": "cancelled", "total": "2.675"}]
    products = [{"name": "Product " + str(i), "sku": "DUPLICATE" if i < 2 else str(i),
        "stock_quantity": i % 4, "total_sales": i * 10} for i in range(8)]
    products += [{"stock_quantity": 5}, {"stock_quantity": None}]
    statuses = [{"Status": "cancelled", "Order_Count": 1, "Total_Revenue": "2.68", "Avg_Order_Value": "2.68"},
        {"Status": "completed", "Order_Count": 2, "Total_Revenue": "10.01", "Avg_Order_Value": "5.01"},
        {"Status": "refunded", "Order_Count": 1, "Total_Revenue": "-1.01", "Avg_Order_Value": "-1.01"}]
    rows = [{"Product_Name": products[i]["name"], "SKU": products[i]["sku"],
        "Stock_Quantity": products[i]["stock_quantity"], "Total_Sales": products[i]["total_sales"]}
        for i in (0, 4, 1, 5, 2, 6, 3, 7)]
    return orders, products, statuses, rows


class OrderAnalysisValidationTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()

    def measure(self, data=None, binding=None):
        return analysis.measure_rows(*(self.data if data is None else data),
            source_binding=analysis.contract_identity() if binding is None else binding)

    def assert_failed(self, data):
        result = self.measure(data)
        self.assertEqual(result["evaluation_status"], "completed")
        self.assertIs(result["rows_passed"], False)
        self.assertIsNone(result["task_passed"])
        self.assertIsNone(result["reward"])

    def test_all_statuses_and_dates_are_included_without_extra_source_fields(self):
        before = deepcopy(self.data)
        result = self.measure()
        self.assertIs(result["rows_passed"], True)
        self.assertEqual(result["measurements"], {"orders": 4, "status_groups": 3, "products": 10, "low_stock_products": 8})
        self.assertEqual(self.data, before)
        self.assertIsNone(result["task_passed"])
        for field in ("calendar_checked", "snapshot_completeness_verified", "original_evaluator_executed", "nemo_task_executed", "training_eligible"):
            self.assertIs(result[field], False)

    def test_wrong_money_inside_original_loose_tolerance_still_fails(self):
        for column in ("Total_Revenue", "Avg_Order_Value"):
            with self.subTest(column=column):
                data = deepcopy(self.data)
                data[2][1][column] = str(Decimal(data[2][1][column]) + Decimal("0.01"))
                self.assert_failed(data)

    def test_round_after_aggregation_and_postgres_numeric_half_ties(self):
        orders = [{"status": "one", "total": "0.004"}, {"status": "one", "total": "0.004"}]
        statuses = [{"Status": "one", "Order_Count": 2, "Total_Revenue": "0.01", "Avg_Order_Value": "0.00"}]
        self.assertTrue(self.measure((orders, [], statuses, []))["rows_passed"])
        expected, _ = analysis.expected_rows(self.data[0], [], source_binding=analysis.contract_identity())
        self.assertEqual(expected[0]["Total_Revenue"], Decimal("2.68"))
        self.assertEqual(expected[-1]["Total_Revenue"], Decimal("-1.01"))
        statuses[0]["Total_Revenue"] = "0.008"
        self.assert_failed((orders, [], statuses, []))

    def test_money_contract_does_not_inherit_callers_decimal_precision_or_traps(self):
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            self.assertTrue(self.measure()["rows_passed"])

    def test_sixth_and_later_products_are_checked_without_sampling_or_tolerance(self):
        for row_index, column, value in ((5, "Total_Sales", 61), (7, "Stock_Quantity", 4), (6, "Product_Name", "Wrong name")):
            with self.subTest(row=row_index, column=column):
                data = deepcopy(self.data)
                data[3][row_index][column] = value
                self.assert_failed(data)

    def test_duplicate_skus_cannot_collapse_or_substitute_product_rows(self):
        data = deepcopy(self.data)
        data[3][2] = deepcopy(data[3][0])
        self.assert_failed(data)
        data = deepcopy(self.data)
        data[3].pop(2)
        self.assert_failed(data)

    def test_missing_extra_duplicate_or_unsorted_status_groups_fail(self):
        for kind in ("missing", "extra", "duplicate", "order"):
            with self.subTest(kind=kind):
                data = deepcopy(self.data)
                if kind == "missing": data[2].pop()
                elif kind == "extra": data[2].append({**data[2][0], "Status": "other"})
                elif kind == "duplicate": data[2][-1] = deepcopy(data[2][0])
                else: data[2].reverse()
                self.assert_failed(data)

    def test_stock_and_name_sort_are_both_enforced(self):
        for i, j in ((0, 2), (0, 1)):
            data = deepcopy(self.data)
            data[3][i], data[3][j] = data[3][j], data[3][i]
            self.assert_failed(data)

    def test_truncation_collisions_preserve_multiplicity_without_inventing_tie_order(self):
        prefix = "\u83dc" * 80
        products = [{"name": prefix + "z", "sku": None, "stock_quantity": 1, "total_sales": 4},
            {"name": prefix + "a", "sku": "", "stock_quantity": 1, "total_sales": 5}]
        rows = [{"Product_Name": prefix, "SKU": "", "Stock_Quantity": 1, "Total_Sales": 5},
            {"Product_Name": prefix, "SKU": None, "Stock_Quantity": 1, "Total_Sales": 4}]
        self.assertTrue(self.measure(([], products, [], rows))["rows_passed"])
        rows[0]["Product_Name"] = prefix + "a"
        self.assert_failed(([], products, [], rows))

    def test_null_stock_is_excluded_negative_stock_and_empty_sku_are_preserved(self):
        products = [{"stock_quantity": None}, {"stock_quantity": 5},
            {"name": "Backorder", "sku": "", "stock_quantity": -2, "total_sales": "0"}]
        rows = [{"Product_Name": "Backorder", "SKU": None, "Stock_Quantity": -2, "Total_Sales": 0}]
        self.assertTrue(self.measure(([], products, [], rows))["rows_passed"])
        self.assertTrue(self.measure(([], [], [], []))["rows_passed"])
        self.assertTrue(self.measure(([], products[:2], [], []))["rows_passed"])

    def test_invalid_source_is_unknown_even_if_workbook_rows_are_malformed(self):
        for field, value in (("total", True), ("total", float("nan")), ("total", Decimal("Infinity")),
                             ("total", None), ("total", 10 ** 5000), ("status", None)):
            with self.subTest(field=field, kind=type(value).__name__):
                orders = [{"status": "completed", "total": "1"}]
                orders[0][field] = value
                with self.assertRaises(ResourceError):
                    analysis.expected_rows(orders, [], source_binding=analysis.contract_identity())
                result = self.measure((orders, [], None, None))
                self.assertEqual(result["evaluation_status"], "error")
                self.assertIsNone(result["rows_passed"])
        for orders, products in ((None, []), ([{"status": "completed"}], []), ([], [{}]),
            ([], [{"stock_quantity": True}]), ([], [{"stock_quantity": 1, "name": "Item", "sku": ""}])):
            self.assertIsNone(self.measure((orders, products, [], []))["rows_passed"])

    def test_invalid_artifact_numeric_and_missing_named_columns_are_failures(self):
        for column, value in (("Order_Count", True), ("Order_Count", 1.5), ("Total_Revenue", float("inf")),
                              ("Avg_Order_Value", "=1+1")):
            data = deepcopy(self.data)
            data[2][0][column] = value
            self.assert_failed(data)
        data = deepcopy(self.data)
        del data[3][0]["SKU"]
        self.assert_failed(data)
        self.assert_failed((self.data[0], self.data[1], None, self.data[3]))

    def test_wrong_public_commit_or_blob_never_produces_row_success(self):
        for field in ("commit", "instruction_git_blob", "evaluation_sha256"):
            binding = analysis.contract_identity()
            binding[field] = "unbound"
            result = self.measure(binding=binding)
            self.assertEqual(result["evaluation_status"], "error")
            self.assertIsNone(result["supplemental_passed"])


def calendar_event(products):
    return {"summary": analysis.MEETING_TITLE,
        "start": {"dateTime": "2026-03-10T10:00:00Z"},
        "end": {"dateTime": "2026-03-10T11:00:00+00:00"},
        "description": "; ".join(p["name"] + ": stock " + str(p["stock_quantity"])
            for p in products if p.get("stock_quantity") is not None and p["stock_quantity"] < 5)}


class OrderAnalysisCalendarTests(unittest.TestCase):
    def setUp(self):
        self.products = fixture()[1]
        self.event = calendar_event(self.products)

    def measure(self, events=None, products=None):
        return analysis.measure_calendar(self.products if products is None else products,
            [self.event] if events is None else events, source_binding=analysis.contract_identity())

    def test_exact_meeting_and_complete_products_accept_offsets_and_unrelated_events(self):
        self.event["start"]["dateTime"] = "2026-03-10T06:00:00-04:00"
        self.event["end"]["dateTime"] = "2026-03-10T07:00:00-04:00"
        result = self.measure([{"summary": "Unrelated"}, self.event, deepcopy(self.event)])
        self.assertIs(result["calendar_passed"], True)
        self.assertEqual(result["measurements"]["described_products"], 8)
        self.assertIsNone(result["reward"])
        self.assertIsNone(result["task_passed"])

    def test_wrong_title_date_end_or_missing_event_is_measurable_failure(self):
        for field, value in (("summary", analysis.MEETING_TITLE + " extra"),
            ("start", {"dateTime": "2026-03-11T10:00:00Z"}),
            ("end", {"dateTime": "2026-03-10T12:00:00Z"}),
            ("start", {"date": "2026-03-10"}), ("end", {"dateTime": "2026-03-10T11:00:00"})):
            event = {**self.event, field: value}
            result = self.measure([event])
            self.assertEqual(result["evaluation_status"], "completed")
            self.assertIs(result["calendar_passed"], False)
        self.assertIs(self.measure([])["calendar_passed"], False)

    def test_every_product_quantity_is_checked_including_later_items(self):
        for old, new in (("Product 7: stock 3", "Product 7: stock 4"),
                         ("; Product 7: stock 3", "")):
            event = {**self.event, "description": self.event["description"].replace(old, new)}
            self.assertIs(self.measure([event])["calendar_passed"], False)

    def test_missing_source_and_conflicting_description_are_unknown(self):
        for events in (None, [{}], [{**self.event, "description": "Product 0 0 1"}],
            [{**self.event, "description": self.event["description"] + "; Product 0: stock 4"}],
            [{k: v for k, v in self.event.items() if k != "description"}]):
            result = analysis.measure_calendar(self.products, events, source_binding=analysis.contract_identity())
            self.assertEqual(result["evaluation_status"], "error")
            self.assertIsNone(result["calendar_passed"])

    def test_description_uses_full_name_and_preserves_quantity_association(self):
        products = [{"name": "Long " + "x" * 90, "sku": "", "stock_quantity": 2, "total_sales": 1},
                    {"name": "Tea, Black", "sku": "", "stock_quantity": -1, "total_sales": 1}]
        event = calendar_event(products)
        self.assertTrue(self.measure([event], products)["calendar_passed"])
        event["description"] = event["description"].replace(products[0]["name"], products[0]["name"][:80])
        self.assertIsNone(self.measure([event], products)["calendar_passed"])

    def test_sql_datetime_rows_and_no_low_stock_products(self):
        event = {"summary": analysis.MEETING_TITLE, "description": None,
            "start_datetime": datetime(2026, 3, 10, 10, tzinfo=timezone.utc),
            "end_datetime": datetime(2026, 3, 10, 11, tzinfo=timezone.utc)}
        self.assertTrue(self.measure([event], [])["calendar_passed"])


@unittest.skipUnless(find_spec("openpyxl"), "openpyxl unavailable in stdlib-only verification")
class OrderAnalysisWorkbookTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / analysis.WORKBOOK_NAME
        self.orders, self.products, self.statuses, self.stocks = fixture()
        self.events = [calendar_event(self.products)]
        self.write()

    def write(self, change=None):
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.remove(workbook.active)
        for (name, headers), rows in zip(analysis.HEADERS.items(), (self.statuses, self.stocks)):
            sheet = workbook.create_sheet(name)
            sheet.append(list(headers))
            for row in rows:
                sheet.append([row[key] for key in headers])
        if change: change(workbook)
        workbook.save(self.path)
        workbook.close()

    def measure(self):
        return analysis.measure_workbook(self.orders, self.products, self.events, self.root,
            source_binding=analysis.contract_identity())

    def rewrite_archive(self, transform, extra=()):
        with ZipFile(self.path) as archive:
            entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
        with ZipFile(self.path, "w", ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, transform(name, data))
            for name, data in extra: archive.writestr(name, data)

    def test_actual_xlsx_and_calendar_pass_without_claiming_complete_task(self):
        result = self.measure()
        self.assertTrue(result["supplemental_passed"])
        self.assertTrue(result["workbook_read"])
        self.assertTrue(result["calendar_checked"])
        self.assertIsNone(result["task_passed"])
        self.assertIsNone(result["reward"])
        self.assertFalse(result["snapshot_completeness_verified"])
        self.assertFalse(result["original_evaluator_executed"])

    def test_headers_sheets_formulas_and_wrong_data_are_rejected(self):
        mutations = [lambda w: setattr(w["Status_Summary"]["A1"], "value", "Wrong"),
            lambda w: setattr(w["Status_Summary"]["B1"], "value", "Status"),
            lambda w: setattr(w["Status_Summary"]["C2"], "value", "=1+1"),
            lambda w: setattr(w["Low_Stock_Products"]["D9"], "value", 999),
            lambda w: w.remove(w["Low_Stock_Products"])]
        for change in mutations:
            self.write(change)
            result = self.measure()
            self.assertEqual(result["evaluation_status"], "completed")
            self.assertIs(result["supplemental_passed"], False)

    def test_forged_small_dimension_cannot_hide_additional_rows(self):
        self.write(lambda w: w["Low_Stock_Products"].append(["Extra", "X", 0, 0]))
        self.rewrite_archive(lambda name, data: re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1:D9"', data)
            if name == "xl/worksheets/sheet2.xml" else data)
        self.assertIs(self.measure()["supplemental_passed"], False)

    def test_missing_malformed_oversized_and_expanded_archives_fail_boundedly(self):
        self.path.unlink()
        self.assertIs(self.measure()["supplemental_passed"], False)
        self.path.write_bytes(b"not an xlsx")
        self.assertIs(self.measure()["supplemental_passed"], False)
        self.path.write_bytes(b"0" * (analysis.MAX_WORKBOOK_BYTES + 1))
        self.assertIs(self.measure()["supplemental_passed"], False)
        self.write()
        self.rewrite_archive(lambda n, d: d, [("large.txt", b"0" * (8 * 1024 * 1024))])
        self.assertIs(self.measure()["supplemental_passed"], False)

    def test_xml_entities_external_members_and_traversal_are_rejected(self):
        for name, body in (("../outside", b"x"), ("xl/externalLinks/item.xml", b"<x/>"),
            ("unsafe.xml", b'<!DOCTYPE x [<!ENTITY e "value">]><x/>'),
            ("unsafe.rels", b'<!DOCTYPE x [<!ENTITY e "value">]><x/>')):
            self.write()
            self.rewrite_archive(lambda n, d: d, [(name, body)])
            self.assertIs(self.measure()["supplemental_passed"], False)

    def test_permission_error_and_untrusted_path_are_unknown(self):
        with patch.object(Path, "open", side_effect=PermissionError("inaccessible")):
            self.assertIsNone(self.measure()["supplemental_passed"])
        with patch.object(analysis, "no_links", side_effect=ValueError("linked")):
            self.assertIsNone(self.measure()["supplemental_passed"])

    def test_source_and_calendar_unknown_take_precedence_over_bad_artifact(self):
        self.path.write_bytes(b"invalid")
        self.events = None
        self.assertIsNone(self.measure()["supplemental_passed"])
        self.events = []
        self.orders = None
        self.assertIsNone(self.measure()["supplemental_passed"])


if __name__ == "__main__":
    unittest.main()
