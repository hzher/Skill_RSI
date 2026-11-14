"""Recompute local customer component evidence without conferring task readiness."""
import hashlib
import json

from adapters import customer_spending_validation as validation
from adapters.customer_report_evaluation import classify_original
from adapters.office_workbook import read_tables
from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_absence, track_input
from training.office_task_review import TASKS
from workenv.core import contained

CHECKS = ("unregistered_component_scope", "two_owned_empty_gsheet_resources", "seven_original_method_schemas",
    "two_missing_workbook_failures", "complete_customer_and_order_pagination", "explicit_basis_success_and_discrepancies",
    "transaction_workbook_roundtrip", "cloud_content_blind_spot_rejected", "workbook_money_tolerance_rejected",
    "restored_component_success", "unallowlisted_mutation_refused", "foreign_ready_marker_unknown",
    "reset_one_preserves_other", "readonly_sources_unchanged", "children_and_postgres_closed", "shared_database_unchanged")
SOURCES = ("scripts/verify_customer_report_components.py", "integrations/customer_report_components.py",
    "integrations/customer_report_audit.py", "adapters/customer_report_evaluation.py", "adapters/customer_spending_validation.py",
    "adapters/office_workbook.py", "adapters/meal_plan_artifacts.py", "adapters/google_sheets_native.py",
    "adapters/woocommerce_native.mjs", "adapters/toolathlon_native.py", "integrations/order_analysis_resource.py",
    "integrations/episode_cluster.py", "integrations/meal_plan_services.py", "integrations/resource_transport.py",
    "training/office_task_review.py", "training/source_api.py", "workenv/process_identity.py", "workenv/postgres_binary.py",
    "workenv/leases.py", "workenv/atomic_io.py", "workenv/core.py", "scripts/verify_calendar_isolation.py")


def audit_components(root, path):
    receipt = read_json(path, root)
    if (receipt.get("schema_version") != "customer-report-original-components-diagnostic-v1" or receipt.get("passed") is not True
        or [c["name"] for c in receipt["checks"]] != list(CHECKS) or any(c["passed"] is not True for c in receipt["checks"])
        or receipt.get("reference_mode") != "project_derived_oracle" or receipt.get("task_passed") is not None or receipt.get("reward") is not None
        or any(receipt.get(k) is not False for k in ("spending_basis_approved", "runtime_profile_registered", "nemo_task_executed",
            "upstream_reference_used", "official_benchmark_score_available", "training_eligible"))
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates"))):
        raise ValueError("Customer component evidence scope changed")
    if set(receipt["source_sha256"]) != set(SOURCES): raise ValueError("Customer component source inventory differs")
    source_binding(root, receipt["source_sha256"])
    retained = receipt["retained_sha256"]
    if not isinstance(retained, dict) or not 20 <= len(retained) <= 512: raise ValueError("Customer retained inventory differs")
    for name, expected in retained.items():
        member = contained(root, name)
        if member.stat().st_size > 16 * 1024 * 1024: raise ValueError("Customer evidence member budget")
        digest = hashlib.sha256(member.read_bytes()).hexdigest()
        if digest != expected: raise ValueError("Customer raw evidence bytes differ")
        track_input(member, root, digest)
    count, cloud_blind, money_blind = 0, False, False
    truth_by_index = {}
    for row in receipt["measurements"]:
        measured = row["result"]
        ref = measured["evaluation_ref"]
        if ref not in retained or read_json(contained(root, ref), root) != measured: raise ValueError("Customer measurement summary changed")
        directory = contained(root, ref).parent
        if hashlib.sha256((directory / "original_evaluation.py").read_bytes()).hexdigest() != TASKS[validation.TASK_ID]["evaluation/main.py"]:
            raise ValueError("Customer evaluator is not the pinned original")
        data = read_json(contained(root, measured["snapshot_ref"]), root)
        truth_by_index.setdefault(row["index"], data)
        supplemental = validation.measure_workbook(data["customers"], data["orders"], directory, data, basis=measured["basis"])
        original = classify_original(measured["original_exit_code"], (directory / "original.stdout.txt").read_text(), (directory / "original.stderr.txt").read_text())
        if (original != measured["original_parser_passed"] or supplemental != measured["supplemental"]
            or measured["reference_mode"] != "project_derived_oracle" or measured["upstream_reference_used"] is not False
            or measured["task_passed"] is not None or measured["reward"] is not None):
            raise ValueError("Customer parser/supplemental recomputation differs")
        oracle = directory / "project-derived-oracle/WC_Customer_Report.xlsx"
        if hashlib.sha256(oracle.read_bytes()).hexdigest() != measured["oracle_sha256"]: raise ValueError("Derived oracle changed")
        expected = validation.expected(data["customers"], data["orders"], basis=measured["basis"])
        tables = read_tables(oracle.parent, oracle.name, validation.HEADERS)
        validation.check_rows(tables["Customer Spending"], expected)
        validation.check_summary(tables["Summary"], expected)
        component = None if original is None or supplemental["supplemental_passed"] is None else bool(original and supplemental["supplemental_passed"])
        if measured["component_measurement_passed"] is not component: raise ValueError("Customer component outcome differs")
        if original is True and supplemental["supplemental_passed"] is False:
            local_tables = read_tables(directory, "WC_Customer_Report.xlsx", validation.HEADERS)
            try: validation.check_rows(local_tables["Customer Spending"], expected)
            except validation.InvalidReport: money_blind = True
            else:
                try: validation.check_rows(validation.cloud_rows(data), expected)
                except validation.InvalidReport: cloud_blind = True
        count += 1
    if count < 8 or not cloud_blind or not money_blind or receipt["shared_before"] != receipt["shared_after"]:
        raise ValueError("Incomplete customer component cases")
    for i, record in enumerate(receipt["profiles"]):
        profile = EpisodeProfile(root, record["resource_id"], record["slot"])
        profile.validate_record(record)
        require_dependents_closed(profile)
        track_absence(profile.data / "postmaster.pid", root)
        journal = read_json(profile.base / "customer-components-run/tool_journal.json", root)
        for method, table in (("woo_customers_list", "customers"), ("woo_orders_list", "orders")):
            calls = [e for e in journal if e["server"] == "woocommerce" and e["name"] == method]
            if not calls or [e["arguments"]["page"] for e in calls] != list(range(1, len(calls) + 1)):
                raise ValueError("Customer original pagination journal differs")
            pages = [json.loads("".join(b["text"] for b in e["response"]["content"] if b["type"] == "text")) for e in calls]
            rows = [r for page in pages for r in page]
            if len(pages[-1]) >= 10 or len({r["id"] for r in rows}) != len(rows) or {r["id"] for r in rows} != {r["id"] for r in truth_by_index[i][table]}:
                raise ValueError("Customer pagination completeness differs")
    return {"checks": len(CHECKS), "measurements_recomputed": count, "reference_mode": "project_derived_oracle",
            "nemo_task_executed": False, "runtime_profiles_added": 0, "official_score_available": False}
