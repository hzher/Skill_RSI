"""Scripted tax-task controller using only supplied resource calls, never a model."""
from decimal import Decimal, ROUND_HALF_UP
import uuid


def solve(post, bound):
    def call(server, name, arguments):
        return post("/tool", {**bound, "request_id": str(uuid.uuid4()), "server": server,
                              "name": name, "arguments": arguments})["result"]
    def money(value):
        return float(value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))
    orders = []
    for status in ("completed", "processing"):
        for page in range(1, 11):
            result = call("woocommerce", "woo_orders_list", {"status": [status], "perPage": 100, "page": page})
            if not isinstance(result, list): raise ValueError("Unexpected original order tool result")
            orders.extend(result)
            if len(result) < 100: break
        else: raise ValueError("Diagnostic pagination budget exhausted")
    if not orders or len({r["id"] for r in orders}) != len(orders): raise ValueError("Unexpected qualifying order set")
    sales, tax, states = Decimal(0), Decimal(0), {}
    for order in orders:
        sale, amount = Decimal(str(order["total"])), Decimal(str(order["total_tax"]))
        sales += sale
        tax += amount
        state = order.get("billing", {}).get("state")
        if state:
            row = states.setdefault(state, [0, Decimal(0), Decimal(0)])
            row[0] += 1
            row[1] += sale
            row[2] += amount
    overall = [["Metric", "Value"], ["Total Orders", len(orders)], ["Total Sales", money(sales)],
               ["Total Tax", money(tax)], ["Effective Tax Rate", money(tax / sales * 100) if sales else 0]]
    rows = [["State", "Order Count", "Total Sales", "Total Tax"]]
    rows.extend([state, row[0], money(row[1]), money(row[2])] for state, row in sorted(states.items(), key=lambda p: (-p[1][2], p[0])))
    post("/write_workbook", {**bound, "request_id": str(uuid.uuid4()), "workbook": {
        "sheets": [{"name": "By State", "rows": rows}, {"name": "Overall", "rows": overall}]}})
    created = call("google_sheet", "create_spreadsheet", {"title": "Tax Summary for Accounting"})
    sid = created["spreadsheetId"]
    tabs = call("google_sheet", "list_sheets", {"spreadsheet_id": sid})
    call("google_sheet", "rename_sheet", {"spreadsheet": sid, "sheet": tabs[0], "new_name": "Summary"})
    call("google_sheet", "update_cells", {"spreadsheet_id": sid, "sheet": "Summary", "range": "A1:B5", "data": overall})
    return {"orders": len(orders), "spreadsheet_id": sid}
