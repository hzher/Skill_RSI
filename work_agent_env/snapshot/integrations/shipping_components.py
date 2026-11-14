"""Shipping thin layer over the existing owned office services."""
from contextlib import asynccontextmanager
import json
import hashlib

from adapters import shipping_validation as validation
from integrations.customer_report_components import CustomerReportComponents, initialize as initialize_office, snapshot as office_snapshot
from training.shipping_task_review import inspect
from workenv.core import ResourceError

TASK_ID = validation.TASK_ID
METHODS = {"woocommerce": ("woo_orders_list", "woo_shipping_zones_list", "woo_shipping_zone_methods_list", "workenv_shipping_zone_locations_get"),
           "google_sheet": ("create_spreadsheet", "list_sheets", "rename_sheet", "update_cells", "get_sheet_data")}


def snapshot(cluster):
    data = office_snapshot(cluster, include_shipping=True)
    data["source_binding"] = validation.source_identity()
    return data


def initialize(root, cluster):
    return initialize_office(root, cluster, task_id=TASK_ID)


class ShippingComponents(CustomerReportComponents):
    task_id, methods, artifact_task, run_name = TASK_ID, METHODS, "shipping", "shipping-components-run"
    shipping_locations = True

    @asynccontextmanager
    async def open(self):
        inspect(self.root)
        async with super().open(): yield self


def check_original_sources(data, actual):
    fields = {"orders": ("status", "total", "billing"), "shipping_zones": ("name",),
              "shipping_zone_locations": ("zone_id", "type", "code"),
              "shipping_zone_methods": ("zone_id", "method_id", "method_title", "settings")}
    for table, columns in fields.items():
        ids = validation.source_rows(actual[table], ("id", *columns))
        wanted = {row["id"]: row for row in data[table]}
        if ids != set(wanted): raise ResourceError("Original shipping inventory truncated or foreign: " + table)
        for row in actual[table]:
            for key in columns:
                left, right = row[key], wanted[row["id"]][key]
                if key == "total": left, right = validation.number(left), validation.number(right)
                if left != right: raise ResourceError("Original shipping field differs: " + table + "." + key)


async def collect_sources(controller, data):
    def decode(result):
        if result.get("isError"): raise ResourceError("Original shipping tool returned an error")
        values = json.loads("".join(b["text"] for b in result["content"] if b["type"] == "text"))
        if not isinstance(values, list): raise ResourceError("Original shipping result is not an array")
        return values
    actual = {"orders": [], "shipping_zone_locations": [], "shipping_zone_methods": []}
    pages = []
    for page in range(1, 65):
        values = decode(await controller.call("woocommerce", "woo_orders_list", {"page": page, "perPage": 10}))
        pages.append({"page": page, "rows": len(values)})
        actual["orders"].extend(values)
        if len(values) < 10: break
    else: raise ResourceError("Shipping order pagination budget exceeded")
    actual["shipping_zones"] = decode(await controller.call("woocommerce", "woo_shipping_zones_list", {}))
    for zone in actual["shipping_zones"]:
        arguments = {"zoneId": validation.integer(zone["id"], minimum=1)}
        actual["shipping_zone_locations"].extend(decode(await controller.call("woocommerce", "workenv_shipping_zone_locations_get", arguments)))
        actual["shipping_zone_methods"].extend(decode(await controller.call("woocommerce", "woo_shipping_zone_methods_list", arguments)))
    # These original zone/method APIs expose no pagination parameters. A result
    # matching only their default first page must never count as complete.
    check_original_sources(data, actual)
    return actual, pages


def sql_truth(cluster, data):
    if snapshot(cluster) != data: raise ResourceError("Shipping SQL input changed")
    connection = cluster.connect()
    try:
        connection.rollback()
        connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with connection.cursor() as cursor:
            cursor.execute("""WITH grouped AS (
                SELECT CASE WHEN billing->>'country'='US' AND billing->>'state'='CA' THEN 'California'
                            WHEN billing->>'country'='US' THEN 'Domestic US' ELSE 'International' END AS zone,
                       count(*) AS cnt, round(sum(total::numeric),2) AS revenue, round(avg(total::numeric),2) AS average
                FROM wc.orders WHERE status IN ('completed','processing') GROUP BY 1)
                SELECT z.name, coalesce(g.cnt,0), coalesce(g.revenue,0), coalesce(g.average,0)
                FROM wc.shipping_zones z LEFT JOIN grouped g ON g.zone=z.name ORDER BY z.name COLLATE "C"
                """)
            rows = [dict(zip(validation.HEADERS["Zone Summary"], row)) for row in cursor.fetchall()]
    finally: connection.close()
    truth = validation.expected(data["orders"], data["shipping_zones"], data["shipping_zone_locations"], data["shipping_zone_methods"], source_binding=data["source_binding"])
    if rows != truth["rows"] or snapshot(cluster) != data: raise ResourceError("Independent shipping SQL/Python aggregate differs")
    return {"rows": rows, "methods": truth["methods"]}


def configuration_digest(data):
    tables = ("shipping_zones", "shipping_zone_locations", "shipping_zone_methods")
    return hashlib.sha256(json.dumps({t: data[t] for t in tables}, sort_keys=True).encode()).hexdigest()
