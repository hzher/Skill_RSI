from copy import deepcopy
import importlib.util
import io
import unittest

from integrations.workbook_tool import render_workbook, validate_workbook


def fixture():
    return {"sheets": [{"name": "By State", "rows": [["State", "Order Count", "Total Sales", "Total Tax"], ["CA", 2, 100.5, 9.2]]},
                       {"name": "Overall", "rows": [["Metric", "Value"], ["Total Orders", 2]]}]}


class WorkbookToolTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("openpyxl"), "Original task artifact dependency not installed")
    def test_literal_values_survive_real_xlsx_roundtrip(self):
        from openpyxl import load_workbook
        raw = render_workbook(fixture())
        workbook = load_workbook(io.BytesIO(raw), data_only=False)
        self.addCleanup(workbook.close)
        self.assertEqual(set(workbook.sheetnames), {"By State", "Overall"})
        self.assertEqual(list(workbook["By State"].values)[1], ("CA", 2, 100.5, 9.2))
        self.assertEqual(workbook["By State"]["C2"].data_type, "n")

    def test_formula_invalid_numbers_and_complex_cells_rejected(self):
        for value in ("=HYPERLINK(\"file:///x\")", float("nan"), float("inf"), 10**400, {}, [1], "bad\x00cell"):
            data = fixture()
            data["sheets"][0]["rows"][1][1] = value
            with self.subTest(value=str(value)[:30]), self.assertRaises(ValueError): validate_workbook(data)

    def test_arbitrary_paths_and_duplicate_sheets_rejected(self):
        for name in ("../outside", "Overall", "Another sheet"):
            data = fixture()
            data["sheets"][0]["name"] = name
            with self.subTest(name=name), self.assertRaises(ValueError): validate_workbook(data)
        data = fixture()
        data["path"] = "outside.xlsx"
        with self.assertRaises(ValueError): validate_workbook(data)

    def test_large_grids_and_unbounded_strings_rejected(self):
        data = fixture()
        data["sheets"][0]["rows"] = [[1] * 16 for _ in range(512)]
        with self.assertRaises(ValueError): validate_workbook(data)
        data = fixture()
        data["sheets"][0]["rows"][0][0] = "x" * 2001
        with self.assertRaises(ValueError): validate_workbook(data)


if __name__ == "__main__": unittest.main()
