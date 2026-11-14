"""Fulfillment-specific inventory over shared office HTTP/lifecycle auditing."""
import json
from adapters.fulfillment_evaluation import episode_result, REFERENCE_MODE, REWARD_VERSION
from integrations.fulfillment_audit import audit_measurement
from integrations.mainline_audit import read_json

CHECKS = ("shared_backend_refused_before_allocation", "two_owned_nemo_resources", "original_methods_and_explicit_diagnostic_profile",
    "cookie_binding_and_mutation_allowlist", "missing_artifacts_are_known_failures", "complete_original_order_pagination",
    "two_project_derived_measurements_pass", "workbook_roundtrip", "workbook_cent_error_rejected_and_isolated",
    "cloud_cent_error_rejected_and_isolated", "deduplicated_write_restores_success", "foreign_ready_marker_is_masked_unknown",
    "pending_write_is_masked_unknown", "closing_one_preserves_other", "meal_task_reuses_released_slot",
    "closed_identity_cannot_reseed", "owned_children_and_postgres_closed", "shared_database_and_other_ledgers_unchanged")
EXTRA_SOURCES = ("scripts/verify_nemo_fulfillment.py", "integrations/fulfillment_runtime_audit.py", "integrations/fulfillment_audit.py",
                 "scripts/verify_calendar_isolation.py", "integrations/office_runtime_audit.py")


def audit_fulfillment(root, path):
    from integrations.office_runtime_audit import audit_report
    return audit_report(root, path, family="fulfillment", task_id="wc-order-fulfillment-tracker", checks=CHECKS,
        extra_sources=EXTRA_SOURCES, episode_result=episode_result, audit_measurement=audit_measurement,
        check_inventory=check_inventory, reward_version=REWARD_VERSION, reference_mode=REFERENCE_MODE)


def audit_lifecycle(root, path):
    from integrations.office_runtime_audit import audit_lifecycle as common_lifecycle
    return common_lifecycle(root, path, family="fulfillment", task_id="wc-order-fulfillment-tracker")


def check_inventory(root, profile, data):
    calls = read_json(profile.base / "fulfillment-components-run/tool_journal.json",root)
    orders = [e for e in calls if e["server"] == "woocommerce"]
    if [e["arguments"]["page"] for e in orders] != list(range(1,len(orders)+1)): raise ValueError("Fulfillment pagination journal differs")
    pages = [json.loads("".join(c["text"] for c in e["response"]["content"] if c["type"] == "text")) for e in orders]
    rows = [r for page in pages for r in page]
    truth = {r["id"]: r for r in data["orders"]}
    if not pages or len(pages[-1]) >= 10 or len({r["id"] for r in rows}) != len(rows) or {r["id"] for r in rows} != set(truth):
        raise ValueError("Fulfillment original pagination incomplete")
    from adapters.fulfillment_validation import number
    if any(r["status"] != truth[r["id"]]["status"] or number(r["total"]) != number(truth[r["id"]]["total"]) for r in rows):
        raise ValueError("Fulfillment original order data differs")
