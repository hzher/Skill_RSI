"""Pure inventory alert measurements; no tools, source discovery or task reward.

The caller must independently prove pagination, SQL snapshot and ownership.
Comparisons use supplied product fields, never the evaluator's fixed ID list.
"""
from collections import Counter
from datetime import datetime, timezone

from adapters.customer_spending_validation import InvalidReport, integer, number, source_rows
from training.inventory_task_review import PIN, TASK_ID, PUBLIC_SHA256
from workenv.core import ResourceError

FILENAME = "out_of_stock_report.txt"
SENDER = "purchasing@ourstore.com"
RECIPIENT = "warehouse@ourstore.com"
SUBJECT = "Out of Stock Alert - March 2026"
ZONE = "America/New_York"
MAX_TEXT_BYTES = 256 * 1024
MAX_MAIL_CHARACTERS = 16384
# March 10 is after the 2026 US DST transition. These are instants for this
# fixed instruction only, not a general-purpose replacement for IANA tzdata.
START = datetime(2026, 3, 10, 13, tzinfo=timezone.utc)
END = datetime(2026, 3, 10, 14, tzinfo=timezone.utc)


def source_identity():
    return {"source": "toolathlon_gym", "commit": PIN, "task_id": TASK_ID,
        "public_instruction_sha256": PUBLIC_SHA256["docs/task.md"],
        "original_evaluator_sha256": PUBLIC_SHA256["evaluation/main.py"],
        "basis": "complete_product_catalog_stock_quantity_equals_zero"}


def line_text(value, error=ResourceError, *, blank=False, limit=4096):
    if (not isinstance(value, str) or len(value) > limit or not blank and not value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise error("inventory_invalid_single_line_text")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise error("inventory_invalid_utf8_text") from exc
    return value


def expected(products, *, source_binding):
    if source_binding != source_identity():
        raise ResourceError("inventory_source_binding_differs")
    source_rows(products, ("id", "name", "sku", "stock_quantity", "categories"))
    rows = []
    for product in products:
        pid = integer(product["id"], minimum=1)
        name = line_text(product["name"])
        sku = line_text(product["sku"], blank=True)
        quantity = number(product["stock_quantity"])
        if quantity != quantity.to_integral_value():
            raise ResourceError("inventory_fractional_stock_policy_unresolved")
        categories = product["categories"]
        if not isinstance(categories, list) or len(categories) > 1024:
            raise ResourceError("inventory_category_shape")
        for category in categories:
            if not isinstance(category, dict) or "name" not in category:
                raise ResourceError("inventory_category_name_missing")
            line_text(category["name"])
        category = categories[0]["name"] if categories else "Uncategorized"
        if quantity == 0:
            rows.append({"id": pid, "name": name, "sku": sku, "category": category})
    rows.sort(key=lambda row: row["id"])
    report = "\n".join(f'{r["id"]} - {r["name"]} - {r["sku"]} - {r["category"]}' for r in rows)
    body = "\n".join([*(f'ID {r["id"]}: {r["name"]} (SKU: {r["sku"]})' for r in rows),
        f"Total out-of-stock products: {len(rows)}"])
    if len(report.encode("utf-8")) > MAX_TEXT_BYTES or len(body) > MAX_MAIL_CHARACTERS:
        raise ResourceError("inventory_expected_output_exceeds_transport_budget")
    events = [{"summary": "Restock: " + r["name"][:50],
        "description": f'Product ID: {r["id"]}. SKU: {r["sku"]}. Category: {r["category"]}. '
            "This product is currently out of stock and needs to be reordered.",
        "start_datetime": START.isoformat(), "end_datetime": END.isoformat(),
        "start_timezone": ZONE, "end_timezone": ZONE} for r in rows]
    return {"products": rows, "report_text": report, "email_body": body, "events": events}


def lines(value, error=InvalidReport):
    if not isinstance(value, str):
        raise error("inventory_output_text_required")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise error("inventory_output_utf8_invalid") from exc
    if len(encoded) > MAX_TEXT_BYTES or any(ord(c) < 32 and c not in "\r\n" or ord(c) == 127 for c in value):
        raise error("inventory_output_text_budget_or_control_character")
    # Permit LF/CRLF and one terminal newline only. Blank/interior extra lines
    # and altered field whitespace remain measurable errors.
    value = value.replace("\r\n", "\n")
    if "\r" in value:
        raise error("inventory_bare_carriage_return")
    if value.endswith("\n"):
        value = value[:-1]
    return value.split("\n") if value else []


def check_report(report, truth):
    if lines(report) != lines(truth["report_text"]):
        raise InvalidReport("inventory_report_rows_values_or_sort_differ")


def instant(value):
    try:
        if isinstance(value, str) and len(value) <= 128:
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Offset required")
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError) as exc:
        raise InvalidReport("inventory_event_timestamp_invalid") from exc


def check_events(events, truth):
    if not isinstance(events, list) or len(events) != len(truth["events"]):
        raise InvalidReport("inventory_exact_event_inventory_required")
    seen = []
    for event in events:
        if not isinstance(event, dict) or not set(truth["events"][0]) <= set(event):
            raise InvalidReport("inventory_event_fields_missing")
        if (event["start_timezone"] != ZONE or event["end_timezone"] != ZONE
                or instant(event["start_datetime"]) != START or instant(event["end_datetime"]) != END):
            raise InvalidReport("inventory_event_time_or_timezone_differs")
        if event.get("status", "confirmed") != "confirmed":
            raise InvalidReport("inventory_event_not_confirmed")
        seen.append((line_text(event["summary"], InvalidReport), line_text(event["description"], InvalidReport, limit=16384)))
    wanted = [(event["summary"], event["description"]) for event in truth["events"]]
    if Counter(seen) != Counter(wanted):
        raise InvalidReport("inventory_event_title_description_or_identity_differs")


def check_mail(messages, sent_log, truth):
    if not isinstance(messages, list) or not isinstance(sent_log, list) or len(messages) != 1 or len(sent_log) != 1:
        raise InvalidReport("inventory_single_message_and_send_log_required")
    row, log = messages[0], sent_log[0]
    if (not isinstance(row, dict) or not {"id", "from_addr", "to_addr", "subject", "body_text"} <= set(row)
            or not isinstance(log, dict) or not {"id", "message_id"} <= set(log)):
        raise InvalidReport("inventory_email_or_send_log_fields_missing")
    mid = integer(row["id"], InvalidReport, minimum=1)
    integer(log["id"], InvalidReport, minimum=1)
    if integer(log["message_id"], InvalidReport, minimum=1) != mid:
        raise InvalidReport("inventory_send_log_message_link_differs")
    if (row["from_addr"] != SENDER or row["to_addr"] != [RECIPIENT] or row["subject"] != SUBJECT
            or any(row.get(k) for k in ("cc_addr", "bcc_addr", "body_html"))):
        raise InvalidReport("inventory_sender_recipient_subject_or_hidden_content")
    if lines(row["body_text"]) != lines(truth["email_body"]):
        raise InvalidReport("inventory_email_product_rows_or_total_differ")


def measure(products, report, events, messages, sent_log, *, source_binding):
    result = {"schema_version": "inventory-alert-pure-contract-v1", "task_id": TASK_ID,
        "contract_source": source_identity(), "supplemental_passed": None, "task_passed": None, "reward": None,
        "training_eligible": False, "nemo_task_executed": False, "original_evaluator_executed": False,
        "source_completeness_verified": False, "resource_ownership_verified": False,
        "empty_inventory_policy": "empty_report_zero_events_single_zero_total_email_project_supplement",
        "text_comparison": "exact_lines_with_optional_terminal_newline_and_crlf",
        "time_policy": "fixed_2026_03_10_new_york_09_to_10_equals_13_to_14_utc",
        "send_log_scope": "single_message_identity_link_only"}
    try:
        truth = expected(products, source_binding=source_binding)
        if report is None or events is None or messages is None or sent_log is None:
            raise ResourceError("inventory_output_snapshot_unavailable")
        check_report(report, truth)
        check_events(events, truth)
        check_mail(messages, sent_log, truth)
        result.update(supplemental_passed=True, evaluation_status="completed", errors=[])
    except InvalidReport as exc:
        result.update(supplemental_passed=False, evaluation_status="completed", errors=[str(exc)])
    except ResourceError as exc:
        result.update(evaluation_status="error", errors=[str(exc)])
    return result
