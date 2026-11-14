"""Customer report measurements with an explicit, non-interchangeable basis.

No upstream references, tool calls, task reward or implicit policy selection.
Source completeness and resource ownership must be attested by the caller.
"""
from collections import Counter
from decimal import Context, Decimal, DecimalException, ROUND_HALF_UP, localcontext

from adapters.office_workbook import InvalidWorkbook, read_tables
from workenv.core import ResourceError

TASK_ID = "wc-customer-spending-report"
HEADERS = {"Customer Spending": ("Customer_ID", "Name", "Email", "Orders", "Total_Spent", "Avg_Order_Value"),
           "Summary": ("Metric", "Value")}
BASES = ("woocommerce_customer_fields", "all_order_rows")
CENT = Decimal("0.01")
MAX_ROWS = 100000


class InvalidReport(ValueError):
    pass


def number(value, error=ResourceError):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise error("invalid_number")
    if isinstance(value, int) and value.bit_length() > 256 or isinstance(value, Decimal) and len(value.as_tuple().digits) > 128:
        raise error("number_budget")
    text = str(value)
    if len(text) > 128: raise error("number_budget")
    try:
        result = Decimal(text)
        if not result.is_finite() or abs(result.adjusted()) > 24 or abs(result.as_tuple().exponent) > 24:
            raise error("number_budget")
        return result
    except DecimalException as exc:
        raise error("invalid_number") from exc


def integer(value, error=ResourceError, *, minimum=0):
    result = number(value, error)
    if result != result.to_integral_value() or not minimum <= result <= 10**12: raise error("invalid_integer")
    return int(result)


def text(value, error=ResourceError, *, blank=True):
    if not isinstance(value, str) or len(value) > 4096 or "\0" in value or not blank and not value:
        raise error("invalid_text")
    return value


def source_rows(rows, fields):
    if not isinstance(rows, list) or len(rows) > MAX_ROWS: raise ResourceError("source_row_budget")
    identities = set()
    for row in rows:
        if not isinstance(row, dict) or not set(fields) <= set(row): raise ResourceError("source_fields_missing")
        identifier = integer(row["id"], minimum=1)
        if identifier in identities: raise ResourceError("duplicate_source_identity")
        identities.add(identifier)
    return identities


def compare_bases(customers, orders):
    """Do not equate the provider's customer counters with order-row aggregates."""
    source_rows(customers, ("id", "first_name", "last_name", "email", "orders_count", "total_spent"))
    source_rows(orders, ("id", "customer_id", "total"))
    grouped = {integer(c["id"], minimum=1): [0, Decimal(0)] for c in customers}
    guests = 0
    with localcontext(Context(prec=80)):
        for row in orders:
            identifier = integer(row["customer_id"])
            amount = number(row["total"])
            if amount < 0: raise ResourceError("negative_order_total")
            if identifier == 0:
                guests += 1
                continue
            if identifier not in grouped: raise ResourceError("order_customer_missing")
            grouped[identifier][0] += 1
            grouped[identifier][1] += amount
        differences = []
        for customer in customers:
            identifier = integer(customer["id"], minimum=1)
            count, amount = integer(customer["orders_count"]), number(customer["total_spent"])
            if amount < 0: raise ResourceError("negative_customer_spending")
            actual = grouped[identifier]
            if count != actual[0] or amount != actual[1]:
                differences.append({"customer_id": identifier, "provider_orders": count, "provider_total": str(amount),
                                    "aggregate_orders": actual[0], "aggregate_total": str(actual[1])})
    return {"different_customers": differences, "guest_orders_excluded": guests, "order_aggregates": grouped,
            "source_completeness_verified": False}


def expected(customers, orders, *, basis):
    if basis not in BASES: raise ResourceError("spending_basis_must_be_explicit")
    comparison = compare_bases(customers, orders)
    rows = []
    with localcontext(Context(prec=80)):
        total = Decimal(0)
        for customer in customers:
            identifier = integer(customer["id"], minimum=1)
            if basis == BASES[0]:
                count, amount = integer(customer["orders_count"]), number(customer["total_spent"])
            else:
                count, amount = comparison["order_aggregates"][identifier]
            name = (text(customer["first_name"]) + " " + text(customer["last_name"])).strip()
            email = text(customer["email"])
            if not count: continue
            if not name: raise ResourceError("active_customer_name_missing")
            total += amount
            rows.append({"Customer_ID": identifier, "Name": name, "Email": email, "Orders": count,
                "Total_Spent": amount.quantize(CENT, rounding=ROUND_HALF_UP),
                "Avg_Order_Value": (amount / count).quantize(CENT, rounding=ROUND_HALF_UP)})
        rows.sort(key=lambda row: (-row["Total_Spent"], row["Customer_ID"]))
        top_names = {row["Name"] for row in rows if row["Total_Spent"] == rows[0]["Total_Spent"]} if rows else {""}
        summary = {"Total_Active_Customers": len(rows), "Total_Spent_All": total.quantize(CENT, rounding=ROUND_HALF_UP),
                   "Top_Customer": rows[0]["Name"] if rows else "",
                   "Avg_Lifetime_Value": (total / len(rows) if rows else Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP)}
    return {"rows": rows, "summary": summary, "allowed_top_names": top_names, "comparison": comparison, "basis": basis}


def canonical_rows(rows):
    if not isinstance(rows, list) or len(rows) > 4096: raise InvalidReport("report_row_budget")
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(HEADERS["Customer Spending"]): raise InvalidReport("report_columns_differ")
        values = (integer(row["Customer_ID"], InvalidReport, minimum=1), text(row["Name"], InvalidReport, blank=False),
            text(row["Email"], InvalidReport), integer(row["Orders"], InvalidReport, minimum=1),
            number(row["Total_Spent"], InvalidReport), number(row["Avg_Order_Value"], InvalidReport))
        if values[4] < 0 or values[5] < 0 or any(v != v.quantize(CENT) for v in values[4:]):
            raise InvalidReport("money_requires_exact_cents")
        result.append(values)
    if len({row[0] for row in result}) != len(result): raise InvalidReport("duplicate_report_customer")
    return result


def check_rows(rows, truth):
    actual, wanted = canonical_rows(rows), canonical_rows(truth["rows"])
    if Counter(actual) != Counter(wanted): raise InvalidReport("customer_set_or_values_differ")
    if [row[4] for row in actual] != sorted((row[4] for row in actual), reverse=True):
        raise InvalidReport("spending_order_differs")
    return actual


def check_summary(rows, truth):
    if not isinstance(rows, list) or len(rows) != 4: raise InvalidReport("summary_metrics_differ")
    actual = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"Metric", "Value"}: raise InvalidReport("summary_columns_differ")
        key = row["Metric"]
        if not isinstance(key, str) or key not in truth["summary"] or key in actual: raise InvalidReport("summary_metrics_differ")
        actual[key] = row["Value"]
    for key, value in actual.items():
        if key == "Top_Customer":
            if not isinstance(value, str) or value not in truth["allowed_top_names"]: raise InvalidReport("top_customer_differs")
        elif key == "Total_Active_Customers":
            if integer(value, InvalidReport) != truth["summary"][key]: raise InvalidReport("summary_count_differs")
        elif number(value, InvalidReport) != truth["summary"][key]: raise InvalidReport("summary_money_differs")


def cloud_rows(snapshot, *, layout="customer_report"):
    if layout == "customer_report":
        title, sheet_title, columns = "Customer Analysis", "Spending", HEADERS["Customer Spending"]
    elif layout == "fulfillment":
        from adapters.fulfillment_validation import HEADERS as fulfillment_headers
        title, sheet_title, columns = "Order Fulfillment Dashboard", "Status Breakdown", fulfillment_headers["Order Status"]
    elif layout == "shipping":
        from adapters.shipping_validation import HEADERS as shipping_headers
        title, sheet_title, columns = "Shipping Zone Dashboard", "Summary", shipping_headers["Zone Summary"]
    else: raise ValueError("Unsupported fixed cloud task layout")
    if not isinstance(snapshot, dict) or not {"spreadsheets", "sheets", "cells"} <= set(snapshot):
        raise ResourceError("cloud_snapshot_missing")
    for name in ("spreadsheets", "sheets", "cells"):
        if not isinstance(snapshot[name], list) or len(snapshot[name]) > MAX_ROWS: raise ResourceError("cloud_snapshot_budget")
    try:
        parents = [row for row in snapshot["spreadsheets"] if row["title"] == title]
        candidates = [(parent, child) for parent in parents for child in snapshot["sheets"]
                      if child["spreadsheet_id"] == parent["id"] and child["title"] == sheet_title]
        if not candidates: raise InvalidReport("cloud_task_sheet_missing")
        # Multiple task outputs require an explicit identity from the resource layer.
        if len(candidates) != 1: raise ResourceError("cloud_task_sheet_ambiguous")
        parent, child = candidates[0]
        cells = {}
        for cell in snapshot["cells"]:
            if cell["spreadsheet_id"] != parent["id"] or cell["sheet_id"] != child["id"]: continue
            row, col = integer(cell["row_index"], InvalidReport), integer(cell["col_index"], InvalidReport)
            if row > 4096 or col >= len(columns) or (row, col) in cells: raise InvalidReport("cloud_grid_shape_or_duplicate")
            if cell.get("formula"): raise InvalidReport("cloud_formulas_forbidden")
            cells[row, col] = cell["value"]
        if tuple(cells.get((0, col)) for col in range(len(columns))) != columns:
            raise InvalidReport("cloud_headers_differ")
        indices = sorted({row for row, _ in cells if row})
        if indices != list(range(1, len(indices) + 1)): raise InvalidReport("cloud_rows_not_contiguous")
        if any((row, col) not in cells for row in indices for col in range(len(columns))): raise InvalidReport("cloud_cells_missing")
        return [dict(zip(columns, (cells[row, col] for col in range(len(columns))))) for row in indices]
    except (KeyError, TypeError) as exc:
        raise ResourceError("cloud_snapshot_incomplete") from exc


def measure(customers, orders, tables, cloud, *, basis=None):
    result = {"schema_version": "customer-spending-contract-v1", "task_id": TASK_ID, "basis": basis,
        "supplemental_passed": None, "task_passed": None, "reward": None, "training_eligible": False,
        "source_completeness_verified": False, "original_evaluator_executed": False, "nemo_task_executed": False}
    try:
        truth = expected(customers, orders, basis=basis)
        result["source_policy_discrepancies"] = truth["comparison"]["different_customers"]
        if not isinstance(tables, dict) or set(tables) != set(HEADERS): raise InvalidReport("task_tables_differ")
        local = check_rows(tables["Customer Spending"], truth)
        check_summary(tables["Summary"], truth)
        remote = check_rows(cloud_rows(cloud), truth)
        if local != remote: raise InvalidReport("local_cloud_row_order_or_values_differ")
        result.update(supplemental_passed=True, evaluation_status="completed", errors=[])
    except (InvalidReport, InvalidWorkbook) as exc:
        result.update(supplemental_passed=False, evaluation_status="completed", errors=[str(exc)])
    except (ResourceError, DecimalException) as exc:
        result.update(evaluation_status="error", errors=[str(exc)])
    return result


def measure_workbook(customers, orders, workspace, cloud, *, basis=None):
    # Validate source and policy first; unavailable input is never a business failure.
    try:
        expected(customers, orders, basis=basis)
        tables = read_tables(workspace, "WC_Customer_Report.xlsx", HEADERS)
    except InvalidWorkbook as exc:
        result = measure(customers, orders, {}, cloud, basis=basis)
        result.update(errors=[str(exc)])
        return result
    except (ResourceError, DecimalException) as exc:
        return {"schema_version": "customer-spending-contract-v1", "task_id": TASK_ID, "basis": basis,
            "evaluation_status": "error", "supplemental_passed": None, "task_passed": None, "reward": None,
            "errors": [str(exc)], "training_eligible": False, "source_completeness_verified": False,
            "original_evaluator_executed": False, "nemo_task_executed": False}
    return measure(customers, orders, tables, cloud, basis=basis)
