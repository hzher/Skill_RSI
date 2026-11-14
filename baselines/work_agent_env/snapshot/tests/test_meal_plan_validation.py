"""Supplemental task checks use explicit fixtures, never task answer workbooks."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from adapters import meal_plan_validation as plan


def fixture():
    recipes = [{"id": "fixture-" + meal, "name": "Fixture " + meal, "prep_time_minutes": None,
                "ingredients": [{"name": meal + " ingredient"}]} for meal in plan.MEALS]
    rows, events = [], []
    for i in range(7):
        day = plan.FIRST_DAY + timedelta(days=i)
        for meal, (clock, prep) in plan.MEALS.items():
            name = "Fixture " + meal
            rows.append({"day": day.isoformat(), "meal_type": meal, "recipe_name": name, "prep_time": prep})
            start = datetime.combine(day, clock, timezone(timedelta(hours=-4)))
            events.append({"id": str(len(events)), "summary": "Cook: " + name,
                           "start": {"dateTime": start.isoformat(), "timeZone": plan.ZONE},
                           "end": {"dateTime": (start + timedelta(hours=1)).isoformat(), "timeZone": plan.ZONE}})
    shopping = [{"ingredient": meal + " ingredient", "quantity": "as needed"} for meal in plan.MEALS]
    return events, rows, shopping, recipes


class MealPlanValidationTests(unittest.TestCase):
    def setUp(self):
        # A fixed-week unit fixture; the real IANA dependency is checked separately.
        zone = patch.object(plan, "ZoneInfo", return_value=timezone(timedelta(hours=-4)))
        zone.start()
        self.addCleanup(zone.stop)
        self.data = fixture()

    def measure(self, data=None): return plan.measure_rows(*(data or self.data))

    def assert_failed(self, data):
        measured = self.measure(data)
        self.assertEqual(measured["evaluation_status"], "completed")
        self.assertIs(measured["task_passed"], False)
        self.assertIsNone(measured["reward"])

    def test_complete_fixture_allows_reused_recipes_and_reports_diversity(self):
        result = self.measure()
        self.assertTrue(result["task_passed"])
        self.assertEqual(result["measurements"], {"meals": 21, "events": 21, "distinct_recipes": 3, "unique_ingredients": 3})
        self.assertFalse(result["original_evaluator_executed"])
        self.assertFalse(result["nemo_task_executed"])
        self.assertFalse(result["training_eligible"])

    def test_duplicate_missing_or_eighth_day_meal_is_rejected(self):
        for kind in ("missing", "duplicate", "eighth_day"):
            data = deepcopy(self.data)
            if kind == "missing": data[1].pop()
            elif kind == "duplicate": data[1][-1] = deepcopy(data[1][0])
            else: data[1][-1]["day"] = "2026-03-16"
            self.assert_failed(data)

    def test_wrong_slot_duration_and_naive_or_wrong_zone_events_are_rejected(self):
        for changes in ({"start": {"dateTime": "2026-03-09T08:00:00-04:00", "timeZone": plan.ZONE}},
                        {"end": {"dateTime": "2026-03-09T09:00:00-04:00", "timeZone": plan.ZONE}},
                        {"start": {"dateTime": "2026-03-09T07:00:00", "timeZone": plan.ZONE}},
                        {"start": {"dateTime": "2026-03-09T07:00:00-04:00", "timeZone": "UTC"}}):
            data = deepcopy(self.data)
            data[0][0].update(changes)
            self.assert_failed(data)

    def test_calendar_recipe_must_match_its_workbook_slot(self):
        data = deepcopy(self.data)
        data[0][0]["summary"] = "Cook: Fixture dinner"
        self.assert_failed(data)

    def test_unknown_recipe_wrong_prep_and_duplicate_event_id_are_rejected(self):
        for kind in ("recipe", "prep", "event_id"):
            data = deepcopy(self.data)
            if kind == "recipe": data[1][0]["recipe_name"] = "Not from public snapshot"
            elif kind == "prep": data[1][0]["prep_time"] = float('nan')
            else: data[0][1]["id"] = data[0][0]["id"]
            self.assert_failed(data)

    def test_shopping_missing_extra_duplicate_and_empty_quantity_are_rejected(self):
        for kind in ("missing", "extra", "duplicate", "quantity"):
            data = deepcopy(self.data)
            if kind == "missing": data[2].pop()
            elif kind == "extra": data[2].append({"ingredient": "unused", "quantity": "as needed"})
            elif kind == "duplicate": data[2].append(deepcopy(data[2][0]))
            else: data[2][0]["quantity"] = " "
            self.assert_failed(data)

    def test_missing_tz_or_unavailable_calendar_snapshot_is_unknown(self):
        with patch.object(plan, "ZoneInfo", side_effect=ZoneInfoNotFoundError("fixture missing IANA data")):
            result = self.measure()
        self.assertEqual(result["evaluation_status"], "error")
        self.assertIsNone(result["task_passed"])
        data = list(self.data)
        data[0] = None
        result = self.measure(data)
        self.assertIsNone(result["task_passed"])

    def test_ambiguous_public_name_requires_optional_recipe_id(self):
        data = deepcopy(self.data)
        data[3].append({**deepcopy(data[3][0]), "id": "fixture-other-breakfast"})
        self.assertIsNone(self.measure(data)["task_passed"])
        for row in data[1]:
            if row["meal_type"] == "breakfast": row["recipe_id"] = "fixture-breakfast"
        self.assertTrue(self.measure(data)["task_passed"])


@unittest.skipUnless(importlib.util.find_spec("openpyxl"), "Optional workbook dependency unavailable")
class MealPlanWorkbookTests(unittest.TestCase):
    def setUp(self):
        from openpyxl import Workbook
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        self.path = self.workspace / "Weekly_Meal_Plan.xlsx"
        self.events, rows, shopping, self.recipes = fixture()
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Meal Plan"
        sheet.append(["Day", "Meal_Type", "Recipe_Name", "Prep_Time"])
        for row in rows: sheet.append(list(row.values()))
        sheet = workbook.create_sheet("Shopping List")
        sheet.append(["Ingredient", "Quantity"])
        for row in shopping: sheet.append(list(row.values()))
        workbook.save(self.path)
        workbook.close()
        zone = patch.object(plan, "ZoneInfo", return_value=timezone(timedelta(hours=-4)))
        zone.start()
        self.addCleanup(zone.stop)

    def test_bounded_workbook_is_measured_and_missing_or_broken_artifact_fails(self):
        self.assertTrue(plan.measure_workbook(self.events, self.workspace, self.recipes)["task_passed"])
        self.path.write_bytes(b"not an XLSX")
        self.assertIs(plan.measure_workbook(self.events, self.workspace, self.recipes)["task_passed"], False)
        self.path.unlink()
        self.assertIs(plan.measure_workbook(self.events, self.workspace, self.recipes)["task_passed"], False)

    def test_formula_and_unbounded_sheet_are_rejected(self):
        from openpyxl import load_workbook
        workbook = load_workbook(self.path)
        workbook["Meal Plan"]["D2"] = "=15"
        workbook.save(self.path)
        workbook.close()
        self.assertIs(plan.measure_workbook(self.events, self.workspace, self.recipes)["task_passed"], False)
        workbook = load_workbook(self.path)
        workbook["Meal Plan"]["D2"] = 15
        workbook["Meal Plan"]["A10000"] = "outside budget"
        workbook.save(self.path)
        workbook.close()
        result = plan.measure_workbook(self.events, self.workspace, self.recipes)
        self.assertEqual(result["errors"], ["workbook_sheet_budget_exceeded"])


class ActualIanaDataTests(unittest.TestCase):
    def test_fixed_week_uses_real_america_new_york_rules_when_available(self):
        try: plan.ZoneInfo(plan.ZONE)
        except ZoneInfoNotFoundError: self.skipTest("Optional IANA timezone data unavailable")
        self.assertTrue(plan.measure_rows(*fixture())["task_passed"])
