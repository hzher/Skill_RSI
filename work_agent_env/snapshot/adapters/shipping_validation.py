"""Public shipping contract; no reference answers, NeMo reward or admission."""
from collections import Counter
from decimal import Context, Decimal, ROUND_HALF_UP, localcontext
import re

from adapters.customer_spending_validation import InvalidReport, integer, number, source_rows, text, cloud_rows
from adapters.office_workbook import InvalidWorkbook, read_tables
from training.office_task_review import TASKS, PIN
from workenv.core import ResourceError

TASK_ID = "wc-shipping-zone-gsheet"
HEADERS = {"Zone Summary": ("Zone_Name", "Order_Count", "Total_Revenue", "Avg_Order_Value"),
           "Methods": ("Zone", "Method", "Details")}
ZONES = ("California", "Domestic US", "International")
CENT = Decimal("0.01")


def source_identity():
    return {"source": "toolathlon_gym", "commit": PIN, "task_id": TASK_ID,
            "public_instruction_sha256": TASKS[TASK_ID]["docs/task.md"],
            "original_evaluator_sha256": TASKS[TASK_ID]["evaluation/main.py"]}


def billing_zone(order):
    billing = order.get("billing")
    if not isinstance(billing, dict): raise ResourceError("shipping_billing_missing")
    country = text(billing.get("country"), blank=False)
    if not re.fullmatch("[A-Z]{2}", country): raise ResourceError("shipping_country_not_canonical")
    if country != "US": return "International"
    state = text(billing.get("state"), blank=False)
    if not re.fullmatch("[A-Z]{2}", state): raise ResourceError("shipping_us_state_not_canonical")
    return "California" if state == "CA" else "Domestic US"


def cost_fields(settings):
    if not isinstance(settings, dict): raise ResourceError("shipping_settings_missing")
    result = {}
    for key in ("cost", "min_amount"):
        if key not in settings or settings[key] is None: continue
        value = settings[key]
        # Some WooCommerce endpoints wrap setting values in {value: ...}.
        if isinstance(value, dict):
            if "value" not in value: raise ResourceError("shipping_cost_value_missing")
            value = value["value"]
        if isinstance(value, str) and value == "": continue
        value = number(value)
        if value < 0: raise ResourceError("shipping_negative_cost")
        result[key] = value
    return result


def expected(orders, zones, locations, methods, *, source_binding):
    if source_binding != source_identity(): raise ResourceError("shipping_source_binding_differs")
    source_rows(orders, ("id", "status", "total"))
    source_rows(zones, ("id", "name"))
    source_rows(locations, ("id", "zone_id", "type", "code"))
    source_rows(methods, ("id", "zone_id", "method_id", "method_title", "settings"))
    names = {integer(z["id"], minimum=1): text(z["name"], blank=False) for z in zones}
    if len(names) != 3 or Counter(names.values()) != Counter(ZONES): raise ResourceError("shipping_zone_inventory_unresolved")
    coverage = set()
    for location in locations:
        parent = integer(location["zone_id"], minimum=1)
        if parent not in names: raise ResourceError("shipping_location_parent_missing")
        kind, code = text(location["type"], blank=False), text(location["code"], blank=False)
        if kind not in ("country", "state", "continent", "postcode"): raise ResourceError("shipping_location_type_unknown")
        item = (parent, kind, code)
        if item in coverage: raise ResourceError("shipping_location_duplicate")
        coverage.add(item)
    if set(names) != {r[0] for r in coverage}: raise ResourceError("shipping_zone_coverage_missing")
    grouped = {name: [0, Decimal(0)] for name in ZONES}
    with localcontext(Context(prec=80)):
        for order in orders:
            status = text(order["status"], blank=False)
            if status != status.lower() or status != status.strip(): raise ResourceError("shipping_status_not_canonical")
            if status not in ("completed", "processing"): continue
            amount = number(order["total"])
            if amount < 0: raise ResourceError("shipping_negative_order_total")
            group = grouped[billing_zone(order)]
            group[0] += 1
            group[1] += amount
        rows = [{"Zone_Name": name, "Order_Count": count, "Total_Revenue": total.quantize(CENT, rounding=ROUND_HALF_UP),
                 "Avg_Order_Value": (total / count if count else Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP)}
                for name, (count, total) in sorted(grouped.items())]
    method_rows = []
    for method in methods:
        parent = integer(method["zone_id"], minimum=1)
        if parent not in names: raise ResourceError("shipping_method_parent_missing")
        mid, title = text(method["method_id"], blank=False), text(method["method_title"], blank=False)
        if title.strip() != title or mid.strip() != mid: raise ResourceError("shipping_method_text_not_canonical")
        costs = cost_fields(method["settings"])
        details = title + "".join("; " + k + "=" + str(v) for k, v in costs.items())
        method_rows.append({"Zone": names[parent], "Method": mid, "Details": details})
    method_rows.sort(key=lambda row: (row["Zone"], row["Method"], row["Details"]))
    return {"rows": rows, "methods": method_rows}


def canonical(rows):
    if not isinstance(rows, list) or len(rows) != 3: raise InvalidReport("shipping_three_zone_rows_required")
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(HEADERS["Zone Summary"]): raise InvalidReport("shipping_summary_columns")
        result.append((text(row["Zone_Name"], InvalidReport, blank=False), integer(row["Order_Count"], InvalidReport),
                       number(row["Total_Revenue"], InvalidReport), number(row["Avg_Order_Value"], InvalidReport)))
    return result


def check_methods(actual, expected_rows):
    if not isinstance(actual, list) or len(actual) != len(expected_rows): raise InvalidReport("shipping_method_instances_differ")
    # Match a multiset: one report row cannot satisfy several method instances.
    unmatched = list(expected_rows)
    for row in actual:
        if not isinstance(row, dict) or set(row) != set(HEADERS["Methods"]): raise InvalidReport("shipping_method_columns")
        zone, method, details = (text(row[key], InvalidReport, blank=False) for key in HEADERS["Methods"])
        matches = []
        for i, wanted in enumerate(unmatched):
            title, *requirements = wanted["Details"].split("; ")
            if zone != wanted["Zone"] or method != wanted["Method"] or title not in details: continue
            valid = True
            available = Counter(number(token, InvalidReport) for token in re.findall(r"(?<![\w.])[+-]?\d+(?:\.\d+)?(?![\w.])", details))
            needed = Counter(number(requirement.split("=", 1)[1]) for requirement in requirements)
            if any(available[value] < count for value, count in needed.items()): valid = False
            if valid: matches.append(i)
        if not matches: raise InvalidReport("shipping_method_title_or_cost_differs")
        # Prefer the longest required title if titles share a substring.
        index = max(matches, key=lambda i: len(unmatched[i]["Details"].split("; ")[0]))
        unmatched.pop(index)


def measure_workbook(data, workspace):
    result = {"schema_version": "shipping-workbook-contract-v1", "task_id": TASK_ID, "supplemental_passed": None,
              "task_passed": None, "reward": None, "training_eligible": False, "nemo_task_executed": False}
    try:
        truth = expected(data["orders"], data["shipping_zones"], data["shipping_zone_locations"], data["shipping_zone_methods"], source_binding=data["source_binding"])
        tables = read_tables(workspace, "Shipping_Analysis.xlsx", HEADERS, max_rows=4096)
        if canonical(tables["Zone Summary"]) != canonical(truth["rows"]): raise InvalidReport("shipping_zone_counts_money_or_sort_differ")
        check_methods(tables["Methods"], truth["methods"])
        if canonical(cloud_rows(data, layout="shipping")) != canonical(truth["rows"]): raise InvalidReport("shipping_cloud_summary_differs")
        result.update(supplemental_passed=True, evaluation_status="completed", errors=[])
    except (InvalidReport, InvalidWorkbook) as exc:
        result.update(supplemental_passed=False, evaluation_status="completed", errors=[str(exc)])
    except (ResourceError, KeyError) as exc:
        result.update(evaluation_status="error", errors=[str(exc)])
    return result
