"""Pure VIP cloud-table and local-mail contract; no runtime admission or reward."""
from collections import Counter
from decimal import Context, Decimal, DecimalException, ROUND_HALF_UP, localcontext
import re

from adapters.customer_spending_validation import InvalidReport, source_rows, number, integer, text
from adapters.email_pg_native import SENDER
from integrations.email_components import validate_arguments
from training.email_task_review import PIN, TASK_ID, BASE, PUBLIC_SHA256
from workenv.core import ResourceError

TITLE = "VIP Customer Tracker"
SUBJECT = "Thank You for Being a VIP Customer!"
HEADERS = {"Top Customers": ("Name", "Email", "Orders_Count", "Total_Spent", "Avg_Order_Value"),
           "Store Summary": ("Metric", "Value")}
CENT = Decimal("0.01")


def source_identity():
    return {"source": "toolathlon_gym", "commit": PIN, "task_id": TASK_ID,
        "public_instruction_sha256": PUBLIC_SHA256[BASE + "docs/task.md"],
        "original_evaluator_sha256": PUBLIC_SHA256[BASE + "evaluation/main.py"], "basis": "woocommerce_customer_fields"}


def expected(customers, *, source_binding):
    if source_binding != source_identity(): raise ResourceError("vip_source_binding_differs")
    source_rows(customers, ("id", "first_name", "last_name", "email", "orders_count", "total_spent"))
    if len(customers) < 10: raise ResourceError("vip_fewer_than_ten_customers_policy_unresolved")
    ranked, emails = [], set()
    with localcontext(Context(prec=80)):
        for customer in customers:
            first = text(customer["first_name"], blank=False).strip()
            last = text(customer["last_name"]).strip()
            address = text(customer["email"], blank=False).strip().lower()
            try: validate_arguments("send_email", {"to": address, "subject": SUBJECT, "body": "source validation"})
            except ValueError as exc: raise ResourceError("vip_source_email_invalid") from exc
            if not first or address in emails: raise ResourceError("vip_source_name_or_unique_email_missing")
            emails.add(address)
            amount = number(customer["total_spent"]); count = integer(customer["orders_count"])
            if amount < 0: raise ResourceError("vip_source_negative_spending")
            ranked.append((amount, {"Name": (first + " " + last).strip(), "Email": address, "Orders_Count": count,
                "Total_Spent": amount.quantize(CENT, rounding=ROUND_HALF_UP),
                "Avg_Order_Value": (amount / count if count else Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP)}, first))
        ranked.sort(key=lambda item: item[0], reverse=True)
        for boundary in (5, 10):
            if len(ranked) > boundary and ranked[boundary - 1][0] == ranked[boundary][0]:
                raise ResourceError("vip_rank_boundary_tie_unresolved")
        total = sum((r[0] for r in ranked), Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP)
        vip = sum((r[0] for r in ranked[:10]), Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP)
        metrics = {"Total_Customers": len(customers), "Total_Revenue": total,
            "VIP_Revenue_Share_Pct": (vip / total * 100 if total else Decimal(0)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP),
            "Avg_Customer_Spend": (total / len(customers)).quantize(CENT, rounding=ROUND_HALF_UP)}
    return {"rows": [r[1] for r in ranked[:10]], "metrics": metrics,
            "recipients": {r[1]["Email"]: r[2] for r in ranked[:5]}}


def check_cloud(cloud, truth):
    if not isinstance(cloud, dict) or set(cloud) != {"title", "sheets"} or cloud["title"] != TITLE:
        raise InvalidReport("vip_spreadsheet_title_or_shape")
    sheets = cloud["sheets"]
    if (not isinstance(sheets, list) or len(sheets) != 2 or any(not isinstance(s, dict) or set(s) != {"title", "headers", "rows"} or not isinstance(s["title"], str) for s in sheets)
        or Counter(s["title"] for s in sheets) != Counter(HEADERS.keys())):
        raise InvalidReport("vip_two_unique_sheets_required")
    by_name = {s["title"]: s for s in sheets}
    for name, headers in HEADERS.items():
        if by_name[name]["headers"] != list(headers): raise InvalidReport("vip_sheet_headers")
    rows = by_name["Top Customers"]["rows"]
    if not isinstance(rows, list) or len(rows) != 10: raise InvalidReport("vip_exactly_ten_rows_required")
    actual, totals = {}, []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(HEADERS["Top Customers"]): raise InvalidReport("vip_top_customer_columns")
        address = text(row["Email"], InvalidReport, blank=False).strip().lower()
        if address in actual: raise InvalidReport("vip_duplicate_customer_row")
        actual[address] = {"Name": text(row["Name"], InvalidReport).strip(), "Email": address,
            "Orders_Count": integer(row["Orders_Count"], InvalidReport), "Total_Spent": number(row["Total_Spent"], InvalidReport),
            "Avg_Order_Value": number(row["Avg_Order_Value"], InvalidReport)}
        totals.append(actual[address]["Total_Spent"])
    if totals != sorted(totals, reverse=True) or actual != {r["Email"]: r for r in truth["rows"]}:
        raise InvalidReport("vip_top_ten_identity_values_or_order")
    summary = by_name["Store Summary"]["rows"]
    if not isinstance(summary, list) or len(summary) != 4: raise InvalidReport("vip_four_summary_metrics_required")
    values = {}
    for row in summary:
        if not isinstance(row, dict) or set(row) != {"Metric", "Value"}: raise InvalidReport("vip_summary_columns")
        key = row["Metric"]
        if not isinstance(key, str) or key not in truth["metrics"] or key in values: raise InvalidReport("vip_summary_metric_duplicate_or_unknown")
        values[key] = integer(row["Value"], InvalidReport) if key == "Total_Customers" else number(row["Value"], InvalidReport)
    if values != truth["metrics"]: raise InvalidReport("vip_summary_values_differ")


def check_mail(messages, sent_log, truth):
    if not isinstance(messages, list) or not isinstance(sent_log, list) or len(messages) != 5 or len(sent_log) != 5:
        raise InvalidReport("vip_exactly_five_messages_and_logs_required")
    seen, ids, log_ids, linked = set(), set(), set(), []
    for row in messages:
        if not isinstance(row, dict) or not {"id", "from_addr", "to_addr", "subject", "body_text"} <= set(row):
            raise InvalidReport("vip_email_fields_missing")
        mid = integer(row["id"], InvalidReport, minimum=1)
        targets = row["to_addr"]
        if not isinstance(targets, list) or len(targets) != 1: raise InvalidReport("vip_email_single_recipient_required")
        address = text(targets[0], InvalidReport, blank=False).strip().lower()
        if address not in truth["recipients"] or address in seen or mid in ids: raise InvalidReport("vip_wrong_or_duplicate_recipient")
        if row["from_addr"] != SENDER or row["subject"] != SUBJECT or any(row.get(k) for k in ("cc_addr", "bcc_addr", "body_html")):
            raise InvalidReport("vip_sender_subject_or_hidden_recipient")
        body = text(row["body_text"], InvalidReport, blank=False).casefold()
        first = truth["recipients"][address].casefold()
        if not re.search(r"(?<!\w)" + re.escape(first) + r"(?!\w)", body): raise InvalidReport("vip_first_name_not_addressed")
        if not re.search(r"\b(?:thank|thanks|thankful)\b", body) or not re.search(r"\b(?:loyalty|loyal|patronage|support)\b", body):
            raise InvalidReport("vip_thanks_or_loyalty_words_missing")
        seen.add(address); ids.add(mid)
    for row in sent_log:
        if not isinstance(row, dict) or not {"id", "message_id"} <= set(row): raise InvalidReport("vip_send_log_fields_missing")
        lid = integer(row["id"], InvalidReport, minimum=1)
        if lid in log_ids: raise InvalidReport("vip_duplicate_send_log_id")
        log_ids.add(lid); linked.append(integer(row["message_id"], InvalidReport, minimum=1))
    if Counter(linked) != Counter(ids): raise InvalidReport("vip_send_log_message_links_differ")


def measure(customers, cloud, messages, sent_log, *, source_binding):
    result = {"schema_version": "vip-email-pure-contract-v1", "task_id": TASK_ID, "contract_source": source_identity(),
        "supplemental_passed": None, "task_passed": None, "reward": None, "training_eligible": False,
        "nemo_task_executed": False, "original_evaluator_executed": False, "source_completeness_verified": False,
        "resource_ownership_verified": False, "mail_body_semantics": "first_name_and_english_lexical_checks_only",
        "send_log_scope": "one_to_one_message_identity_links_only", "rounding_policy": "decimal_half_up_project_supplement"}
    try:
        truth = expected(customers, source_binding=source_binding)
        if cloud is None or messages is None or sent_log is None: raise ResourceError("vip_output_snapshot_unavailable")
        check_cloud(cloud, truth); check_mail(messages, sent_log, truth)
        result.update(supplemental_passed=True, evaluation_status="completed", errors=[])
    except InvalidReport as exc:
        result.update(supplemental_passed=False, evaluation_status="completed", errors=[str(exc)])
    except (ResourceError, DecimalException) as exc:
        result.update(evaluation_status="error", errors=[str(exc)])
    return result
