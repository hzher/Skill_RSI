"""Inventory office contract fixtures; no original answers, PG or tool calls."""
from copy import deepcopy
import unittest

from adapters import inventory_alert_validation as contract
from workenv.core import ResourceError


def products():
    return [{"id": 42, "name": "Tea - Large", "sku": "T42", "stock_quantity": 0,
             "categories": [{"name": "Food"}, {"name": "Ignore second"}]},
            {"id": 11, "name": "In stock", "sku": "S11", "stock_quantity": 2, "categories": []},
            {"id": 7, "name": "Mug", "sku": "", "stock_quantity": "0", "categories": []},
            {"id": 21, "name": "Backorder", "sku": "B21", "stock_quantity": -1, "categories": []}]


def fixture():
    # Literal oracle for invented IDs, deliberately unrelated to the original
    # evaluator's five IDs. Do not use expected() to manufacture test answers.
    report = "7 - Mug -  - Uncategorized\n42 - Tea - Large - T42 - Food\n"
    events = [{"summary": "Restock: " + name,
        "description": f"Product ID: {pid}. SKU: {sku}. Category: {category}. "
            "This product is currently out of stock and needs to be reordered.",
        "start_datetime": "2026-03-10T09:00:00-04:00", "end_datetime": "2026-03-10T10:00:00-04:00",
        "start_timezone": "America/New_York", "end_timezone": "America/New_York"}
        for pid, name, sku, category in [(7, "Mug", "", "Uncategorized"), (42, "Tea - Large", "T42", "Food")]]
    messages = [{"id": 91, "from_addr": "purchasing@ourstore.com", "to_addr": ["warehouse@ourstore.com"],
        "subject": "Out of Stock Alert - March 2026",
        "body_text": "ID 7: Mug (SKU: )\nID 42: Tea - Large (SKU: T42)\nTotal out-of-stock products: 2\n"}]
    return [products(), report, events, messages, [{"id": 101, "message_id": 91}]]


class InventoryContractTests(unittest.TestCase):
    def measure(self, values=None, binding=None):
        return contract.measure(*(values if values is not None else fixture()),
            source_binding=contract.source_identity() if binding is None else binding)

    def assert_failed(self, values):
        result = self.measure(values)
        self.assertIs(result["supplemental_passed"], False, result)
        self.assertEqual(result["evaluation_status"], "completed")
        self.assertIsNone(result["task_passed"])
        self.assertIsNone(result["reward"])

    def assert_unknown(self, values):
        result = self.measure(values)
        self.assertIsNone(result["supplemental_passed"], result)
        self.assertEqual(result["evaluation_status"], "error")
        self.assertIsNone(result["reward"])

    def test_literal_office_fixture_passes_without_acquiring_runtime_authority(self):
        result = self.measure()
        self.assertIs(result["supplemental_passed"], True)
        for key in ("training_eligible", "nemo_task_executed", "original_evaluator_executed",
                    "source_completeness_verified", "resource_ownership_verified"):
            self.assertIs(result[key], False)
        self.assertIsNone(result["task_passed"])
        self.assertIsNone(result["reward"])

    def test_input_not_mutated_and_source_order_does_not_determine_output_order(self):
        values = fixture(); before = deepcopy(values)
        self.assertTrue(self.measure(values)["supplemental_passed"])
        self.assertEqual(values, before)
        values[0].reverse(); values[2].reverse()
        self.assertTrue(self.measure(values)["supplemental_passed"])

    def test_stock_exactly_zero_first_category_and_uncategorized_are_independent(self):
        truth = contract.expected(products(), source_binding=contract.source_identity())
        self.assertEqual(truth["products"], [
            {"id": 7, "name": "Mug", "sku": "", "category": "Uncategorized"},
            {"id": 42, "name": "Tea - Large", "sku": "T42", "category": "Food"}])
        self.assertNotIn("Ignore second", truth["report_text"])

    def test_source_binding_drift_keeps_outcome_unknown(self):
        for key in contract.source_identity():
            binding = contract.source_identity(); binding[key] = "wrong"
            result = self.measure(binding=binding)
            self.assertIsNone(result["supplemental_passed"])
            self.assertEqual(result["evaluation_status"], "error")

    def test_missing_fields_duplicate_ids_and_unavailable_catalog_are_unknown(self):
        for field in ("id", "name", "sku", "stock_quantity", "categories"):
            values = fixture(); del values[0][0][field]; self.assert_unknown(values)
        values = fixture(); values[0].append(deepcopy(values[0][0])); self.assert_unknown(values)
        for invalid in (None, {}, "catalog", [None]):
            values = fixture(); values[0] = invalid; self.assert_unknown(values)

    def test_invalid_stock_is_not_silently_treated_as_nonzero(self):
        for invalid in (None, True, "NaN", "Infinity", "bad", 0.5, {}, 10**100):
            values = fixture(); values[0][0]["stock_quantity"] = invalid
            with self.subTest(invalid=repr(invalid)): self.assert_unknown(values)

    def test_source_category_and_text_ambiguity_remain_unknown(self):
        for invalid in (None, {}, [None], [{}], [{"name": ""}], [{"name": "bad\ncategory"}]):
            values = fixture(); values[0][0]["categories"] = invalid; self.assert_unknown(values)
        for field, invalid in (("name", ""), ("name", "bad\nline"), ("name", "\ud800"),
                               ("sku", None), ("sku", "x\0y"), ("id", True), ("id", 0)):
            values = fixture(); values[0][0][field] = invalid; self.assert_unknown(values)

    def test_missing_snapshot_is_unknown_but_present_wrong_shape_is_business_failure(self):
        for index in range(1, 5):
            values = fixture(); values[index] = None; self.assert_unknown(values)
            values = fixture(); values[index] = {} if index != 1 else b"not decoded"
            self.assert_failed(values)

    def test_report_omission_duplicate_wrong_name_sku_category_and_sort_fail(self):
        source = fixture()[1]
        for wrong in (source.splitlines()[0], source + source.splitlines()[0],
                      source.replace("Mug", "Wrong"), source.replace("T42", "wrong"),
                      source.replace("Food", "Ignore second"), "\n".join(reversed(source.splitlines())),
                      source + "11 - In stock - S11 - Uncategorized\n"):
            values = fixture(); values[1] = wrong; self.assert_failed(values)

    def test_crlf_and_optional_terminal_newline_only(self):
        values = fixture(); values[1] = values[1].replace("\n", "\r\n")
        values[3][0]["body_text"] = values[3][0]["body_text"].rstrip("\n")
        self.assertTrue(self.measure(values)["supplemental_passed"])
        for wrong in (fixture()[1] + "\n", "\n" + fixture()[1], fixture()[1].replace("Mug", "Mug "),
                      fixture()[1].replace("\n", "\r"), fixture()[1] + "\0", "\udfff"):
            values = fixture(); values[1] = wrong; self.assert_failed(values)

    def test_calendar_requires_exact_inventory_and_description(self):
        for wrong in ([], fixture()[2] * 2, [fixture()[2][0]] * 2):
            values = fixture(); values[2] = wrong; self.assert_failed(values)
        for key, value in (("summary", "Restock: Wrong"), ("description", "Product ID: 7. SKU: wrong"),
                           ("description", fixture()[2][0]["description"].replace("Uncategorized", "Other"))):
            values = fixture(); values[2][0][key] = value; self.assert_failed(values)
        values = fixture(); del values[2][0]["description"]; self.assert_failed(values)
        values = fixture(); values[2][0]["status"] = "cancelled"; self.assert_failed(values)

    def test_product_name_truncates_fifty_characters_not_fifty_title_characters(self):
        values = fixture(); name = "A" * 49 + "\u8336" + "Z"
        values[0][2]["name"] = name
        values[1] = values[1].replace("Mug", name)
        values[3][0]["body_text"] = values[3][0]["body_text"].replace("Mug", name)
        values[2][0]["summary"] = "Restock: " + "A" * 49 + "\u8336"
        self.assertTrue(self.measure(values)["supplemental_passed"])
        values[2][0]["summary"] += "Z"; self.assert_failed(values)

    def test_fixed_dst_instant_duration_and_both_timezone_fields(self):
        values = fixture(); values[2][0]["start_datetime"] = "2026-03-10T13:00:00Z"
        values[2][0]["end_datetime"] = "2026-03-10T14:00:00+00:00"
        self.assertTrue(self.measure(values)["supplemental_passed"])
        for field, wrong in (("start_datetime", "2026-03-10T09:00:00-05:00"),
            ("end_datetime", "2026-03-10T10:30:00-04:00"), ("start_datetime", "2026-03-11T09:00:00-04:00"),
            ("start_datetime", "2026-03-10T09:00:00"), ("end_datetime", None),
            ("start_timezone", "UTC"), ("end_timezone", "US/Eastern")):
            values = fixture(); values[2][0][field] = wrong; self.assert_failed(values)

    def test_single_exact_sender_recipient_subject_and_no_hidden_content(self):
        for field, wrong in (("from_addr", "vip-program@store.example.com"),
            ("to_addr", ["warehouse@ourstore.com", "other@example.com"]),
            ("to_addr", "warehouse@ourstore.com"), ("to_addr", ["xwarehouse@ourstore.com"]),
            ("subject", "Out of Stock Alert - March"), ("cc_addr", ["other@example.com"]),
            ("bcc_addr", ["hidden@example.com"]), ("body_html", "<p>different</p>")):
            values = fixture(); values[3][0][field] = wrong; self.assert_failed(values)
        values = fixture(); values[3] *= 2; self.assert_failed(values)

    def test_email_rows_sort_full_names_sku_and_exact_total(self):
        body = fixture()[3][0]["body_text"]
        for wrong in (body.replace("Mug", "Other"), body.replace("T42", "wrong"),
            body.replace("products: 2", "products: 20"), "\n".join(reversed(body.splitlines())),
            body + "Extra instructions", body.replace("ID 42", "ID 420")):
            values = fixture(); values[3][0]["body_text"] = wrong; self.assert_failed(values)

    def test_send_log_missing_duplicate_wrong_identity_or_fields_fail(self):
        for log in ([], [{"id": 101, "message_id": 92}], [{"id": True, "message_id": 91}],
                    [{"id": 101}], [{"id": 101, "message_id": 91}] * 2):
            values = fixture(); values[4] = log; self.assert_failed(values)
        values = fixture(); values[3][0]["id"] = False; self.assert_failed(values)

    def test_zero_out_of_stock_has_explicit_project_policy(self):
        values = [[], "", [], [{"id": 1, "from_addr": "purchasing@ourstore.com",
            "to_addr": ["warehouse@ourstore.com"], "subject": "Out of Stock Alert - March 2026",
            "body_text": "Total out-of-stock products: 0"}], [{"id": 2, "message_id": 1}]]
        result = self.measure(values)
        self.assertTrue(result["supplemental_passed"])
        self.assertIn("project_supplement", result["empty_inventory_policy"])
        values[0] = [products()[1]]
        self.assertTrue(self.measure(values)["supplemental_passed"])
        values[3] = []; self.assert_failed(values)

    def test_source_transport_budget_is_unknown_and_oversized_output_is_failure(self):
        values = fixture(); values[0] = [{**products()[0], "id": n + 1, "name": "x" * 4096} for n in range(5)]
        self.assert_unknown(values)
        values = fixture(); values[1] = "\u8336" * (contract.MAX_TEXT_BYTES // 3 + 1)
        self.assert_failed(values)


if __name__ == "__main__": unittest.main()
