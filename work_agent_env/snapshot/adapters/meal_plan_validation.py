"""Supplementary contract for the public howtocook-meal-plan-gcal instruction.

This measures supplied artifacts/state, without running an agent, original
evaluator, NeMo task, or training. Recipe data must come from the verified public
snapshot. Quantity arithmetic, ingredient aliases and subjective meal suitability
are deliberately not inferred from free text.
"""
from datetime import date, datetime, time, timedelta, timezone
import math
from pathlib import Path
import re
import unicodedata
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workenv.core import ResourceError, no_links

TASK_ID = "howtocook-meal-plan-gcal"
INSTRUCTION_BLOB = "8ba609ce13cb5d794448cca814a6f9618fe67bef"
INSTRUCTION_PATH = "tasks/finalpool/" + TASK_ID + "/docs/task.md"
FIRST_DAY = date(2026, 3, 9)
ZONE = "America/New_York"
MEALS = {"breakfast": (time(7), 15), "lunch": (time(11, 30), 30), "dinner": (time(18), 45)}
HEADERS = {"Meal Plan": ("day", "meal_type", "recipe_name", "prep_time"), "Shopping List": ("ingredient", "quantity")}


class InvalidPlan(ValueError):
    """A measurable state/artifact fails the declared task contract."""


def normalized(value):
    if not isinstance(value, str): raise InvalidPlan("expected_text")
    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


def day_value(value):
    if isinstance(value, datetime): return value.date()
    if isinstance(value, date): return value
    text = normalized(value)
    names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    if text in names: return FIRST_DAY + timedelta(days=names.index(text))
    if text in [name[:3] for name in names]: return FIRST_DAY + timedelta(days=[name[:3] for name in names].index(text))
    try: return date.fromisoformat(text)
    except ValueError as exc: raise InvalidPlan("invalid_day") from exc


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)): raise InvalidPlan("invalid_prep_time")
    try: result = float(value)
    except (ValueError, OverflowError) as exc: raise InvalidPlan("invalid_prep_time") from exc
    if not math.isfinite(result) or result < 0: raise InvalidPlan("invalid_prep_time")
    return result


def recipe_index(recipes):
    if not isinstance(recipes, list) or not 1 <= len(recipes) <= 4096: raise ResourceError("public_recipe_snapshot_unavailable")
    result = {}
    for recipe in recipes:
        if not isinstance(recipe, dict) or not isinstance(recipe.get("name"), str) or not isinstance(recipe.get("ingredients"), list):
            raise ResourceError("public_recipe_snapshot_invalid")
        key = normalized(recipe["name"])
        if not key: raise ResourceError("public_recipe_name_empty")
        result.setdefault(key, []).append(recipe)
    return result


def _measure(events, meal_rows, shopping_rows, recipes, zone):
    if not isinstance(events, list) or len(events) > 128: raise ResourceError("calendar_snapshot_unavailable_or_unbounded")
    if not isinstance(meal_rows, list) or not isinstance(shopping_rows, list): raise InvalidPlan("invalid_workbook_rows")
    if len(meal_rows) != 21 or len(events) != 21: raise InvalidPlan("exactly_21_meals_and_events_required")
    if not 1 <= len(shopping_rows) <= 4096: raise InvalidPlan("invalid_shopping_row_count")
    index = recipe_index(recipes)
    expected_slots = {(FIRST_DAY + timedelta(days=i), meal) for i in range(7) for meal in MEALS}
    meals, ingredients, selected = {}, set(), set()
    for row in meal_rows:
        try:
            slot = (day_value(row["day"]), normalized(row["meal_type"]))
            name = normalized(row["recipe_name"])
            prep = number(row["prep_time"])
        except (KeyError, TypeError) as exc: raise InvalidPlan("invalid_meal_row") from exc
        if slot not in expected_slots or slot in meals: raise InvalidPlan("missing_duplicate_or_out_of_week_meal")
        matches = index.get(name, [])
        if not matches: raise InvalidPlan("recipe_name_not_in_public_snapshot")
        # Some upstream names collide. An optional Recipe_ID column disambiguates
        # them without inventing name aliases or silently choosing one recipe.
        if row.get("recipe_id") is not None:
            matches = [r for r in matches if r.get("id") == row["recipe_id"]]
            if not matches: raise InvalidPlan("recipe_id_name_mismatch")
        if len(matches) != 1: raise ResourceError("ambiguous_recipe_name_requires_recipe_id")
        recipe = matches[0]
        expected_prep = recipe.get("prep_time_minutes")
        if expected_prep is None: expected_prep = MEALS[slot[1]][1]
        try: expected_prep = number(expected_prep)
        except InvalidPlan as exc: raise ResourceError("public_recipe_prep_time_invalid") from exc
        if not math.isclose(prep, expected_prep, abs_tol=0.01): raise InvalidPlan("prep_time_differs_from_recipe_or_default")
        meals[slot] = name
        selected.add(recipe.get("id", name))
        for ingredient in recipe["ingredients"]:
            if not isinstance(ingredient, dict) or not isinstance(ingredient.get("name"), str): raise ResourceError("public_recipe_ingredient_invalid")
            key = normalized(ingredient["name"])
            if not key: raise ResourceError("public_recipe_ingredient_empty")
            ingredients.add(key)
    event_slots, event_ids = set(), set()
    for event in events:
        try:
            sid = event["id"]
            summary = event["summary"]
            start_raw, end_raw = event["start"], event["end"]
            if start_raw.get("timeZone") != ZONE or end_raw.get("timeZone") != ZONE: raise InvalidPlan("calendar_time_zone_differs")
            start, end = [datetime.fromisoformat(raw["dateTime"].replace("Z", "+00:00")) for raw in (start_raw, end_raw)]
            if start.utcoffset() is None or end.utcoffset() is None: raise InvalidPlan("calendar_time_requires_offset")
            local = start.astimezone(zone)
            meal = next((name for name, (clock, _) in MEALS.items() if local.time().replace(tzinfo=None) == clock), None)
            slot = (local.date(), meal)
            if not isinstance(sid, str) or not sid or sid in event_ids: raise InvalidPlan("calendar_event_id_missing_or_duplicate")
            if slot not in expected_slots or slot in event_slots: raise InvalidPlan("calendar_meal_slot_missing_duplicate_or_out_of_week")
            if end.astimezone(timezone.utc) - start.astimezone(timezone.utc) != timedelta(hours=1): raise InvalidPlan("calendar_duration_not_one_hour")
            if not isinstance(summary, str) or not normalized(summary).startswith("cook:"): raise InvalidPlan("calendar_summary_requires_cook_prefix")
            if normalized(summary).split(":", 1)[1].strip() != meals[slot]: raise InvalidPlan("calendar_recipe_differs_from_workbook")
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            if isinstance(exc, InvalidPlan): raise
            raise InvalidPlan("invalid_calendar_event") from exc
        event_ids.add(sid)
        event_slots.add(slot)
    shopping = set()
    for row in shopping_rows:
        try:
            key = normalized(row["ingredient"])
            quantity = row["quantity"]
        except (KeyError, TypeError) as exc: raise InvalidPlan("invalid_shopping_row") from exc
        if not key or key in shopping: raise InvalidPlan("shopping_ingredient_empty_or_duplicate")
        if isinstance(quantity, bool) or quantity is None or (isinstance(quantity, str) and not quantity.strip()): raise InvalidPlan("shopping_quantity_missing")
        if not isinstance(quantity, (str, int, float)) or (isinstance(quantity, (int, float)) and (not math.isfinite(quantity) or quantity <= 0)):
            raise InvalidPlan("shopping_quantity_invalid")
        shopping.add(key)
    if shopping != ingredients: raise InvalidPlan("shopping_ingredient_coverage_differs")
    return {"meals": len(meals), "events": len(event_slots), "distinct_recipes": len(selected), "unique_ingredients": len(ingredients)}


def result(passed, *, error=None, unknown=False, measurements=None):
    return {"schema_version": "meal-plan-supplement-v1", "evaluation_status": "error" if unknown else "completed",
            "task_passed": None if unknown else passed, "supplemental_passed": None if unknown else passed,
            "error_category": "verifier_infrastructure" if unknown else None, "errors": [error] if error else [],
            "measurements": measurements or {}, "reward": None, "score": None,
            "original_evaluator_executed": False, "nemo_task_executed": False, "training_eligible": False}


def measure_rows(events, meal_rows, shopping_rows, recipes):
    try:
        zone = ZoneInfo(ZONE)
        return result(True, measurements=_measure(events, meal_rows, shopping_rows, recipes, zone))
    except InvalidPlan as exc: return result(False, error=str(exc))
    except (ResourceError, ZoneInfoNotFoundError) as exc: return result(None, error=str(exc), unknown=True)


def read_workbook(workspace):
    """Read a bounded artifact, without evaluating formulas or following links."""
    try: from openpyxl import load_workbook
    except ImportError as exc: raise ResourceError("openpyxl_unavailable") from exc
    workspace = Path(workspace).absolute()
    path = workspace / "Weekly_Meal_Plan.xlsx"
    try: no_links(path, workspace)
    except ValueError as exc: raise ResourceError("workbook_resource_path_untrusted") from exc
    if not path.exists(): raise InvalidPlan("workbook_missing")
    if not path.is_file() or path.stat().st_size > 1024 * 1024: raise InvalidPlan("workbook_file_budget_exceeded")
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 128 or sum(i.file_size for i in infos) > 8 * 1024 * 1024: raise InvalidPlan("workbook_uncompressed_budget_exceeded")
            for info in infos:
                if (info.flag_bits & 1 or ".." in info.filename.replace("\\", "/").split("/")
                    or "externallinks/" in info.filename.casefold() or "vbaproject" in info.filename.casefold()):
                    raise InvalidPlan("workbook_external_or_unsupported_member")
                if info.filename.endswith(".xml"):
                    xml = archive.read(info)
                    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper(): raise InvalidPlan("workbook_xml_entities_forbidden")
        workbook = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    except (BadZipFile, ValueError, KeyError, SyntaxError) as exc:
        if isinstance(exc, InvalidPlan): raise
        raise InvalidPlan("workbook_content_invalid") from exc
    try:
        mapping = {normalized(name): name for name in workbook.sheetnames}
        if len(mapping) != len(workbook.sheetnames): raise InvalidPlan("ambiguous_sheet_names")
        outputs = []
        for name, required in HEADERS.items():
            if normalized(name) not in mapping: raise InvalidPlan("workbook_required_sheet_missing")
            sheet = workbook[mapping[normalized(name)]]
            budget = 129 if name == "Meal Plan" else 4097
            if sheet.max_row is None or sheet.max_column is None or sheet.max_row > budget or sheet.max_column > 16:
                raise InvalidPlan("workbook_sheet_budget_exceeded")
            rows = list(sheet.iter_rows(max_row=budget, max_col=min(sheet.max_column, 16)))
            header = [re.sub(r"[\s_]+", "_", normalized(c.value)) if c.value is not None else "" for c in rows[0]]
            if any(header.count(key) != 1 for key in required): raise InvalidPlan("workbook_required_header_missing_or_duplicate")
            if header.count("recipe_id") > 1: raise InvalidPlan("workbook_duplicate_recipe_id_header")
            values = []
            for row in rows[1:]:
                if any(cell.data_type == "f" for cell in row): raise InvalidPlan("workbook_formulas_forbidden")
                if all(cell.value is None for cell in row): continue
                values.append({key: row[i].value for i, key in enumerate(header) if key in (*required, "recipe_id")})
            outputs.append(values)
        return outputs
    finally: workbook.close()


def measure_workbook(events, workspace, recipes):
    try:
        ZoneInfo(ZONE)  # Missing IANA data must not become a task failure.
        meals, shopping = read_workbook(workspace)
        return measure_rows(events, meals, shopping, recipes)
    except InvalidPlan as exc: return result(False, error=str(exc))
    except (ResourceError, ZoneInfoNotFoundError, OSError) as exc: return result(None, error=type(exc).__name__, unknown=True)
