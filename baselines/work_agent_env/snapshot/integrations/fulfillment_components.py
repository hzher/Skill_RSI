"""Owned fulfillment components; deliberately separate from NeMo activation."""
from adapters import fulfillment_validation as validation
from integrations.customer_report_components import CustomerReportComponents, initialize as initialize_office, snapshot
from workenv.core import ResourceError

TASK_ID = validation.TASK_ID
METHODS = {"woocommerce": ("woo_orders_list",),
           "google_sheet": ("create_spreadsheet", "list_sheets", "rename_sheet", "update_cells", "get_sheet_data")}


def initialize(root, cluster):
    return initialize_office(root, cluster, task_id=TASK_ID)


class FulfillmentComponents(CustomerReportComponents):
    task_id, methods, artifact_task, run_name = TASK_ID, METHODS, "fulfillment", "fulfillment-components-run"


def sql_truth(cluster, data):
    """Independent PostgreSQL numeric aggregate, checked against complete rows.

    This is a project oracle, never an upstream reference answer. Both snapshots
    surround the aggregate so a concurrent source/cloud edit invalidates it.
    """
    cluster.require_owner()
    if snapshot(cluster) != data: raise ResourceError("Fulfillment aggregate input changed")
    connection = cluster.connect()
    try:
        connection.rollback()
        connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT status, count(*), round(sum(total::numeric),2), round(avg(total::numeric),2) "
                           "FROM wc.orders GROUP BY status ORDER BY status COLLATE \"C\"")
            rows = [dict(zip(validation.HEADERS["Order Status"], row)) for row in cursor.fetchall()]
            cursor.execute("SELECT count(*), count(*) FILTER (WHERE status='completed'), "
                           "count(*) FILTER (WHERE status IN ('pending','processing','on-hold')), "
                           "count(*) FILTER (WHERE status IN ('cancelled','failed')) FROM wc.orders")
            total, completed, pending, cancelled = cursor.fetchone()
            cursor.execute("SELECT CASE WHEN %s=0 THEN 0::numeric ELSE round(100::numeric*%s/%s,2) END, "
                           "CASE WHEN %s=0 THEN 0::numeric ELSE round(100::numeric*%s/%s,2) END",
                           (total, completed, total, total, cancelled, total))
            fulfillment_rate, cancellation_rate = cursor.fetchone()
    finally:
        connection.close()
    summary = {"Total_Orders": total, "Completed_Orders": completed, "Fulfillment_Rate": fulfillment_rate,
               "Pending_Processing_Count": pending, "Cancellation_Count": cancelled, "Cancellation_Rate": cancellation_rate}
    python_rows, python_summary = validation.expected(data["orders"], source_binding=validation.source_identity())
    if rows != python_rows or summary != python_summary or snapshot(cluster) != data:
        raise ResourceError("Independent fulfillment SQL and complete Python rows differ")
    return {"rows": rows, "summary": summary}
