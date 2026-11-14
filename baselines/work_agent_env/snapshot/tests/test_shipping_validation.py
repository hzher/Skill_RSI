"""Shipping boundaries: billing classification, zero zones and method instances."""
from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adapters import shipping_validation as v
from adapters import meal_plan_artifacts as artifacts
from adapters.customer_report_evaluation import render_tables
from adapters.shipping_evaluation import classify_original
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile
from workenv.core import ResourceError, save


def sources():
    zones = [{"id": i+1, "name": name} for i, name in enumerate(v.ZONES)]
    locations = [{"id": i+1, "zone_id": i+1, "type": "country", "code": code} for i, code in enumerate(("US", "US", "GB"))]
    orders = [{"id": 1, "status": "completed", "total": "10.005", "billing": {"country": "US", "state": "CA"}},
              {"id": 2, "status": "processing", "total": "20", "billing": {"country": "US", "state": "NY"}},
              {"id": 3, "status": "completed", "total": "30", "billing": {"country": "GB", "state": "CA"}, "shipping": {"country": "US", "state": "CA"}},
              {"id": 4, "status": "cancelled", "total": "999", "billing": None}]
    methods = [{"id": 1, "zone_id": 1, "method_id": "flat_rate", "method_title": "Standard", "settings": {"cost": "5.99"}},
               {"id": 2, "zone_id": 1, "method_id": "flat_rate", "method_title": "Express", "settings": {"cost": "9.99"}},
               {"id": 3, "zone_id": 2, "method_id": "local_pickup", "method_title": "Store Pickup", "settings": {"cost": 0}},
               {"id": 4, "zone_id": 3, "method_id": "free_shipping", "method_title": "Free Shipping", "settings": {"min_amount": {"value": "100"}}}]
    return {"orders": orders, "shipping_zones": zones, "shipping_zone_locations": locations,
            "shipping_zone_methods": methods, "source_binding": v.source_identity()}


def truth(data):
    return v.expected(data["orders"], data["shipping_zones"], data["shipping_zone_locations"], data["shipping_zone_methods"], source_binding=data["source_binding"])


class ShippingRowsTests(unittest.TestCase):
    def setUp(self): self.data = sources()

    def test_billing_over_shipping_status_filter_sort_and_exact_rounding(self):
        rows = truth(self.data)["rows"]
        self.assertEqual([r["Zone_Name"] for r in rows], list(v.ZONES))
        self.assertEqual([r["Order_Count"] for r in rows], [1, 1, 1])
        self.assertEqual([r["Total_Revenue"] for r in rows], [Decimal("10.01"), Decimal("20"), Decimal("30")])

    def test_zero_filled_regions_and_empty_orders(self):
        self.data["orders"] = self.data["orders"][:1]
        self.assertEqual([r["Order_Count"] for r in truth(self.data)["rows"]], [1, 0, 0])
        self.data["orders"] = []
        self.assertTrue(all(r["Total_Revenue"] == r["Avg_Order_Value"] == 0 for r in truth(self.data)["rows"]))

    def test_unknown_country_state_and_missing_billing_are_not_international_defaults(self):
        for billing in (None, {}, {"country": "us", "state": "CA"}, {"country": "US"}, {"country": "US", "state": "California"}):
            self.data = sources(); self.data["orders"][0]["billing"] = billing
            with self.assertRaises(ResourceError): truth(self.data)

    def test_duplicate_sources_and_foreign_location_or_method_parent_unknown(self):
        for table in ("orders", "shipping_zones", "shipping_zone_locations", "shipping_zone_methods"):
            self.data = sources(); self.data[table].append(deepcopy(self.data[table][0]))
            with self.assertRaises(ResourceError): truth(self.data)
        for table in ("shipping_zone_locations", "shipping_zone_methods"):
            self.data = sources(); self.data[table][0]["zone_id"] = 99
            with self.assertRaises(ResourceError): truth(self.data)

    def test_unknown_zone_set_missing_coverage_and_wrong_public_binding_unknown(self):
        for mutate in (lambda d: d["shipping_zones"][0].update(name="Unreviewed"), lambda d: d["shipping_zone_locations"].pop(), lambda d: d.update(source_binding={})):
            self.data = sources(); mutate(self.data)
            with self.assertRaises(ResourceError): truth(self.data)

    def test_duplicate_method_identifier_is_not_deduplicated(self):
        rows = truth(self.data)["methods"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(r["Method"] == "flat_rate" for r in rows), 2)
        v.check_methods(list(reversed(rows)), rows)
        with self.assertRaises(v.InvalidReport): v.check_methods(rows[:-1], rows)

    def test_original_inventory_default_page_truncation_is_unknown(self):
        from integrations.shipping_components import check_original_sources
        check_original_sources(self.data, self.data)
        for table in ("orders", "shipping_zones", "shipping_zone_locations", "shipping_zone_methods"):
            partial = deepcopy(self.data); partial[table].pop()
            with self.assertRaises(ResourceError): check_original_sources(self.data, partial)

    def test_original_method_cost_or_order_address_difference_unknown(self):
        from integrations.shipping_components import check_original_sources
        partial = deepcopy(self.data); partial["shipping_zone_methods"][0]["settings"]["cost"] = "999"
        with self.assertRaises(ResourceError): check_original_sources(self.data, partial)
        partial = deepcopy(self.data); partial["orders"][0]["billing"]["state"] = "NY"
        with self.assertRaises(ResourceError): check_original_sources(self.data, partial)

    def test_zero_cost_nested_threshold_and_freeform_cost_details(self):
        rows = truth(self.data)["methods"]
        actual = deepcopy(rows)
        pickup = next(r for r in actual if r["Method"] == "local_pickup")
        self.assertIn("cost=0", pickup["Details"])
        pickup["Details"] = "Store Pickup (free, $0.00)"
        v.check_methods(actual, rows)
        pickup["Details"] = "Store Pickup"
        with self.assertRaises(v.InvalidReport): v.check_methods(actual, rows)

    def test_title_wrong_cost_and_threshold_substring_are_rejected(self):
        rows = truth(self.data)["methods"]
        for bad in ("Free Shipping; min_amount=1100", "Free Shipping; min_amount=99", "Other title; min_amount=100"):
            actual = deepcopy(rows)
            next(r for r in actual if r["Method"] == "free_shipping")["Details"] = bad
            with self.assertRaises(v.InvalidReport): v.check_methods(actual, rows)

    def test_missing_nonfinite_or_negative_settings_are_unknown(self):
        for settings in (None, {"cost": "NaN"}, {"cost": -1}, {"min_amount": {"title": "missing value"}}):
            self.data = sources(); self.data["shipping_zone_methods"][0]["settings"] = settings
            with self.assertRaises(ResourceError): truth(self.data)

    def test_original_summary_classifier_keeps_infrastructure_unknown(self):
        self.assertIs(classify_original(0, "  Overall: PASS\n", ""), True)
        self.assertIs(classify_original(1, "  Overall: FAIL\n", ""), False)
        self.assertIsNone(classify_original(1, "[FAIL] DB connect\n  Overall: FAIL\n", ""))
        self.assertIsNone(classify_original(1, "  Overall: FAIL\n", "Traceback"))
        self.assertIsNone(classify_original(0, "  Overall: PASS\n  Overall: FAIL\n", ""))


@unittest.skipUnless(importlib.util.find_spec("openpyxl"), "Optional XLSX dependency")
class ShippingWorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = sources(); wanted = truth(self.data)
        self.tables = {"Zone Summary": wanted["rows"], "Methods": wanted["methods"]}
        self.data.update(spreadsheets=[{"id": "own", "title": "Shipping Zone Dashboard"}],
                         sheets=[{"id": "tab", "spreadsheet_id": "own", "title": "Summary"}])
        grid = [list(v.HEADERS["Zone Summary"]), *[[r[k] for k in v.HEADERS["Zone Summary"]] for r in wanted["rows"]]]
        self.data["cells"] = [{"spreadsheet_id": "own", "sheet_id": "tab", "row_index": i, "col_index": j, "value": str(value)}
                              for i, row in enumerate(grid) for j, value in enumerate(row)]

    def write(self): render_tables(self.root / "Shipping_Analysis.xlsx", v.HEADERS, self.tables)
    def measure(self): return v.measure_workbook(self.data, self.root)

    def test_real_xlsx_and_cloud_pass_without_task_reward(self):
        self.write(); result = self.measure()
        self.assertIs(result["supplemental_passed"], True); self.assertIsNone(result["reward"])

    def test_missing_output_false_but_missing_source_unknown(self):
        self.assertIs(self.measure()["supplemental_passed"], False)
        self.data.pop("shipping_zone_methods")
        self.assertIsNone(self.measure()["supplemental_passed"])

    def test_cent_error_and_wrong_sort_are_business_failures(self):
        self.tables["Zone Summary"][0]["Total_Revenue"] += Decimal(".01"); self.write()
        self.assertIs(self.measure()["supplemental_passed"], False)
        self.tables["Zone Summary"][0]["Total_Revenue"] -= Decimal(".01"); self.tables["Zone Summary"].reverse(); self.write()
        self.assertIs(self.measure()["supplemental_passed"], False)

    def test_method_cost_omission_and_duplicate_instance_fail(self):
        rows = self.tables["Methods"]
        next(r for r in rows if r["Method"] == "local_pickup")["Details"] = "Store Pickup"
        self.write(); self.assertIs(self.measure()["supplemental_passed"], False)
        self.tables["Methods"] = truth(self.data)["methods"]; self.tables["Methods"][0] = deepcopy(self.tables["Methods"][1])
        self.write(); self.assertIs(self.measure()["supplemental_passed"], False)

    def test_cloud_wrong_title_parent_amount_formula_and_ambiguity(self):
        self.write(); original = deepcopy(self.data)
        for mutate in (lambda d: d["spreadsheets"][0].update(title="Other"), lambda d: d["sheets"][0].update(spreadsheet_id="foreign"),
                       lambda d: d["cells"][6].update(value="99"), lambda d: d["cells"][6].update(formula="=99")):
            self.data = deepcopy(original); mutate(self.data)
            self.assertIs(self.measure()["supplemental_passed"], False)
        self.data = deepcopy(original); self.data["sheets"].append(deepcopy(self.data["sheets"][0]))
        self.assertIsNone(self.measure()["supplemental_passed"])

    def test_actual_formula_and_wrong_header_rejected(self):
        from openpyxl import load_workbook
        self.write()
        workbook = load_workbook(self.root / "Shipping_Analysis.xlsx"); workbook["Zone Summary"]["C2"] = "=1+1"
        workbook.save(self.root / "Shipping_Analysis.xlsx"); workbook.close()
        self.assertIs(self.measure()["supplemental_passed"], False)
        self.write()
        workbook = load_workbook(self.root / "Shipping_Analysis.xlsx"); workbook["Methods"]["A1"] = "wrong"
        workbook.save(self.root / "Shipping_Analysis.xlsx"); workbook.close()
        self.assertIs(self.measure()["supplemental_passed"], False)

    def test_owned_transaction_wrong_marker_and_interrupted_replace(self):
        profile = EpisodeProfile(self.root, "9d8c116b-0f7c-4c88-b690-4183972c37cd", 0)
        workspace = profile.base / "shipping-workspace"; workspace.mkdir(parents=True)
        save(profile.base / "cluster_owner.json", profile.record())
        marker = profile.base / "shipping-initialized.json"
        save(marker, {"state": "ready", "task_id": v.TASK_ID, "profile": profile.record()})
        cluster = EpisodeCluster(profile); cluster.started = True
        payload = json.loads(json.dumps(self.tables, default=float))
        record = artifacts.write_report(self.root, cluster, payload, task="shipping")
        raw = (workspace / "Shipping_Analysis.xlsx").read_bytes()
        payload["Zone Summary"][0]["Order_Count"] = 7
        with patch.object(artifacts, "replace_atomic", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError): artifacts.write_report(self.root, cluster, payload, task="shipping")
        self.assertEqual(raw, (workspace / "Shipping_Analysis.xlsx").read_bytes())
        self.assertTrue((profile.base / "shipping-artifact-history" / (record["sha256"]+".xlsx")).exists())
        with self.assertRaises(ResourceError): artifacts.require_no_pending_writes(cluster, task="shipping")
        intent = next(p for p in profile.base.glob("shipping-artifact-write-*.json") if json.loads(p.read_text())["state"] == "prepared")
        reconciled = artifacts.reconcile_write(self.root, cluster, intent.name, task="shipping")
        self.assertEqual(reconciled["decision"], "not_committed"); self.assertIs(reconciled["replay_performed"], False)
        save(marker, {"state": "ready", "task_id": "wc-order-fulfillment-tracker", "profile": profile.record()})
        with self.assertRaises(ResourceError): artifacts.read_report(self.root, cluster, task="shipping")
