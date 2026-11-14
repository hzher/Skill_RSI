"""Pure public fulfillment-report contract, without task activation or rewards."""
from collections import Counter
from decimal import Context, Decimal, DecimalException, ROUND_HALF_UP, localcontext

from adapters.customer_spending_validation import source_rows, number, integer, text, InvalidReport
from adapters.office_workbook import InvalidWorkbook, read_tables
from training.office_task_review import TASKS, PIN
from workenv.core import ResourceError

TASK_ID = "wc-order-fulfillment-tracker"
HEADERS = {"Order Status": ("Status", "Order_Count", "Total_Revenue", "Avg_Order_Value"),
           "Fulfillment Summary": ("Metric", "Value")}
CENT = Decimal("0.01")
COUNT_METRICS = {"Total_Orders", "Completed_Orders", "Pending_Processing_Count", "Cancellation_Count"}


def source_identity():
    return {"source": "toolathlon_gym", "commit": PIN, "task_id": TASK_ID,
            "public_instruction_sha256": TASKS[TASK_ID]["docs/task.md"],
            "original_evaluator_sha256": TASKS[TASK_ID]["evaluation/main.py"]}


def expected(orders, *, source_binding):
    if source_binding != source_identity(): raise ResourceError("fulfillment_source_binding_differs")
    source_rows(orders, ("id", "status", "total"))
    grouped = {}
    with localcontext(Context(prec=80)):
        for order in orders:
            status = text(order["status"], blank=False)
            if status != status.lower() or status.strip() != status: raise ResourceError("source_status_not_canonical")
            amount = number(order["total"])
            count, total = grouped.get(status, (0, Decimal(0)))
            grouped[status] = count + 1, total + amount
        rows = [{"Status": status, "Order_Count": count,
                 "Total_Revenue": total.quantize(CENT, rounding=ROUND_HALF_UP),
                 "Avg_Order_Value": (total / count).quantize(CENT, rounding=ROUND_HALF_UP)}
                for status, (count, total) in sorted(grouped.items())]
        total = len(orders)
        count = lambda statuses: sum(grouped.get(status, (0, 0))[0] for status in statuses)
        completed = count(("completed",))
        cancellation = count(("cancelled", "failed"))
        percent = lambda value: (Decimal(value) / total * 100 if total else Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP)
        summary = {"Total_Orders": total, "Completed_Orders": completed, "Fulfillment_Rate": percent(completed),
                   "Pending_Processing_Count": count(("pending", "processing", "on-hold")),
                   "Cancellation_Count": cancellation, "Cancellation_Rate": percent(cancellation)}
    return rows, summary


def canonical(rows):
    if not isinstance(rows, list) or len(rows) > 256: raise InvalidReport("fulfillment_status_row_budget")
    values = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(HEADERS["Order Status"]): raise InvalidReport("fulfillment_status_columns")
        values.append((text(row["Status"], InvalidReport, blank=False), integer(row["Order_Count"], InvalidReport, minimum=1),
                       number(row["Total_Revenue"], InvalidReport), number(row["Avg_Order_Value"], InvalidReport)))
    return values


def measure_rows(orders, tables, cloud_status_rows, *, source_binding):
    result = {"schema_version": "fulfillment-row-contract-v1", "task_id": TASK_ID, "contract_source": source_identity(),
        "supplemental_passed": None, "task_passed": None, "reward": None, "training_eligible": False,
        "nemo_task_executed": False, "original_evaluator_executed": False,
        "source_completeness_verified": False, "cloud_title_or_ownership_checked": False,
        "empty_order_rate_policy": "zero_for_empty_fixture"}
    try:
        rows, metrics = expected(orders, source_binding=source_binding)
        if cloud_status_rows is None: raise ResourceError("cloud_rows_unavailable")
        if not isinstance(tables, dict) or set(tables) != set(HEADERS): raise InvalidReport("fulfillment_task_tables")
        wanted, local, cloud = canonical(rows), canonical(tables["Order Status"]), canonical(cloud_status_rows)
        if local != wanted: raise InvalidReport("all_status_counts_money_or_sort_differ")
        if cloud != local: raise InvalidReport("cloud_status_rows_differ")
        summary = tables["Fulfillment Summary"]
        if not isinstance(summary, list) or len(summary) != len(metrics): raise InvalidReport("fulfillment_summary_metrics")
        keys, actual = [], {}
        for row in summary:
            if not isinstance(row, dict) or set(row) != {"Metric", "Value"}: raise InvalidReport("fulfillment_summary_columns")
            key = row["Metric"]
            if not isinstance(key, str) or key not in metrics: raise InvalidReport("fulfillment_summary_metric_unknown")
            keys.append(key)
            actual[key] = integer(row["Value"], InvalidReport) if key in COUNT_METRICS else number(row["Value"], InvalidReport)
        if Counter(keys) != Counter(metrics.keys()) or actual != metrics: raise InvalidReport("fulfillment_summary_counts_or_rates")
        result.update(supplemental_passed=True, evaluation_status="completed", errors=[])
    except (InvalidReport, InvalidWorkbook) as exc:
        result.update(supplemental_passed=False, evaluation_status="completed", errors=[str(exc)])
    except (ResourceError, DecimalException) as exc:
        result.update(evaluation_status="error", errors=[str(exc)])
    return result


def measure_workbook(orders, workspace, cloud_status_rows, *, source_binding):
    try:
        expected(orders, source_binding=source_binding)
        tables = read_tables(workspace, "Fulfillment_Report.xlsx", HEADERS, max_rows=256)
    except InvalidWorkbook as exc:
        result = measure_rows(orders, {}, cloud_status_rows, source_binding=source_binding)
        if result["supplemental_passed"] is False: result["errors"] = [str(exc)]
        return result
    except (ResourceError, DecimalException) as exc:
        result = measure_rows([], {}, None, source_binding=source_binding)
        result.update(supplemental_passed=None, evaluation_status="error", errors=[str(exc)])
        return result
    return measure_rows(orders, tables, cloud_status_rows, source_binding=source_binding)
