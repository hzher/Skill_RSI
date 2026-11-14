"""Recompute fulfillment component measurements from retained original bytes."""
import hashlib
import json

from adapters import fulfillment_validation as validation
from adapters.fulfillment_evaluation import classify_original, measure_snapshot, REFERENCE_MODE, profile_registered
from adapters.office_workbook import read_tables
from integrations.customer_report_audit import SOURCES as CUSTOMER_SOURCES
from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_absence, track_input
from training.office_task_review import TASKS
from workenv.core import contained

CHECKS = ("component_run_without_nemo_binding", "two_owned_empty_gsheet_resources", "six_original_method_schemas",
    "two_missing_workbook_failures", "complete_order_pagination", "independent_sql_python_and_original_parser_success",
    "transaction_workbook_roundtrip", "cloud_content_blind_spot_rejected", "workbook_money_tolerance_rejected",
    "restored_component_success", "unallowlisted_mutation_refused", "foreign_task_marker_unknown",
    "unresolved_write_blocks_measurement", "reset_one_preserves_other", "readonly_sources_unchanged",
    "children_and_postgres_closed", "shared_database_unchanged")
SOURCES = tuple(dict.fromkeys((*CUSTOMER_SOURCES, "integrations/fulfillment_components.py",
    "integrations/fulfillment_audit.py", "adapters/fulfillment_evaluation.py", "adapters/fulfillment_validation.py",
    "scripts/verify_fulfillment_components.py")))


def audit_components(root, path):
    receipt = read_json(path, root)
    if (receipt.get("schema_version") != "fulfillment-original-components-diagnostic-v1" or receipt.get("passed") is not True
        or [c["name"] for c in receipt["checks"]] != list(CHECKS) or any(c["passed"] is not True for c in receipt["checks"])
        or receipt.get("reference_mode") != REFERENCE_MODE or receipt.get("task_passed") is not None or receipt.get("reward") is not None
        or receipt.get("runtime_profile_registered") is not profile_registered(root)
        or any(receipt.get(k) is not False for k in ("nemo_task_executed", "upstream_reference_used",
            "official_benchmark_score_available", "training_eligible"))
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates"))):
        raise ValueError("Fulfillment component scope differs")
    if set(receipt["source_sha256"]) != set(SOURCES): raise ValueError("Fulfillment source inventory differs")
    source_binding(root, receipt["source_sha256"])
    retained = receipt["retained_sha256"]
    if not isinstance(retained, dict) or not 20 <= len(retained) <= 512: raise ValueError("Fulfillment retained inventory differs")
    for name, expected in retained.items():
        member = contained(root, name)
        if member.stat().st_size > 16 * 1024 * 1024: raise ValueError("Fulfillment evidence member budget")
        digest = hashlib.sha256(member.read_bytes()).hexdigest()
        if digest != expected: raise ValueError("Fulfillment raw evidence bytes differ")
        track_input(member, root, digest)
    count, cloud_blind, money_blind = 0, False, False
    truth_by_index = {}
    for row in receipt["measurements"]:
        measured = row["result"]
        ref = measured["evaluation_ref"]
        if ref not in retained or read_json(contained(root, ref), root) != measured: raise ValueError("Fulfillment measurement summary changed")
        checked = audit_measurement(root, measured)
        data, original, supplemental, rows = (checked[k] for k in ("data", "original", "supplemental", "rows"))
        directory = checked["directory"]
        workbook = directory / "Fulfillment_Report.xlsx"
        if measured["snapshot_ref"] not in retained: raise ValueError("Fulfillment source snapshot is not retained")
        truth_by_index.setdefault(row["index"], data)
        if data["readonly_sha256"] != truth_by_index[row["index"]]["readonly_sha256"]: raise ValueError("Fulfillment read-only input changed")
        if original is True and supplemental["supplemental_passed"] is False:
            local = read_tables(directory, workbook.name, validation.HEADERS, max_rows=256)
            if validation.canonical(local["Order Status"]) != validation.canonical(rows): money_blind = True
            else: cloud_blind = True
        count += 1
    if count < 10 or not cloud_blind or not money_blind or receipt["shared_before"] != receipt["shared_after"]:
        raise ValueError("Fulfillment component cases incomplete")
    if len(receipt["profiles"]) != 2: raise ValueError("Fulfillment requires two owned resource profiles")
    for i, record in enumerate(receipt["profiles"]):
        profile = EpisodeProfile(root, record["resource_id"], record["slot"])
        profile.validate_record(record)
        require_dependents_closed(profile)
        track_absence(profile.data / "postmaster.pid", root)
        journal = read_json(profile.base / "fulfillment-components-run/tool_journal.json", root)
        calls = [e for e in journal if e["server"] == "woocommerce" and e["name"] == "woo_orders_list"]
        if not calls or [e["arguments"]["page"] for e in calls] != list(range(1, len(calls) + 1)):
            raise ValueError("Fulfillment original pagination journal differs")
        pages = [json.loads("".join(b["text"] for b in e["response"]["content"] if b["type"] == "text")) for e in calls]
        rows = [r for page in pages for r in page]
        expected = {r["id"]: r for r in truth_by_index[i]["orders"]}
        if len(pages[-1]) >= 10 or len({r["id"] for r in rows}) != len(rows) or {r["id"] for r in rows} != set(expected):
            raise ValueError("Fulfillment pagination completeness differs")
        for row in rows:
            if row["status"] != expected[row["id"]]["status"] or validation.number(row["total"]) != validation.number(expected[row["id"]]["total"]):
                raise ValueError("Fulfillment original tool values differ from SQL rows")
    return {"checks": len(CHECKS), "measurements_recomputed": count, "reference_mode": REFERENCE_MODE,
            "nemo_task_executed": False, "runtime_profiles_added": 0, "official_score_available": False}


def audit_measurement(root, measured):
    ref = measured["evaluation_ref"]
    if read_json(contained(root, ref), root) != measured: raise ValueError("Fulfillment measurement summary changed")
    directory = contained(root, ref).parent
    if hashlib.sha256((directory / "original_evaluation.py").read_bytes()).hexdigest() != TASKS[validation.TASK_ID]["evaluation/main.py"]:
        raise ValueError("Fulfillment evaluator is not the pinned original")
    data = read_json(contained(root, measured["snapshot_ref"]), root)
    supplemental = measure_snapshot(data, directory)
    original = classify_original(measured["original_exit_code"], (directory / "original.stdout.txt").read_text(), (directory / "original.stderr.txt").read_text())
    if (original != measured["original_parser_passed"] or supplemental != measured["supplemental"]
        or measured["supplemental_passed"] is not supplemental["supplemental_passed"]
        or measured["reference_mode"] != REFERENCE_MODE
        or measured.get("runtime_profile_registered") is not profile_registered(root)
        or any(measured.get(k) is not False for k in ("upstream_reference_used", "official_benchmark_score_available", "training_eligible"))
        or any(measured.get(k) is not True for k in ("sql_python_agree", "source_completeness_verified", "cloud_ownership_checked"))
        or measured["task_passed"] is not None or measured["reward"] is not None):
        raise ValueError("Fulfillment measurement recomputation differs")
    rows, summary = validation.expected(data["orders"], source_binding=validation.source_identity())
    sql_expected = json.loads(json.dumps({"rows": rows, "summary": summary}, default=str))
    if read_json(directory / "sql_truth.json", root) != sql_expected: raise ValueError("Fulfillment independent SQL truth differs")
    oracle = directory / "project-derived-oracle/Fulfillment_Report.xlsx"
    if hashlib.sha256(oracle.read_bytes()).hexdigest() != measured["oracle_sha256"]: raise ValueError("Fulfillment SQL oracle changed")
    tables = read_tables(oracle.parent, oracle.name, validation.HEADERS, max_rows=256)
    if validation.measure_rows(data["orders"], tables, rows, source_binding=validation.source_identity())["supplemental_passed"] is not True:
        raise ValueError("Fulfillment derived workbook differs from independent truth")
    workbook = directory / "Fulfillment_Report.xlsx"
    if (hashlib.sha256(workbook.read_bytes()).hexdigest() if workbook.exists() else None) != measured["workbook_sha256"]:
        raise ValueError("Fulfillment workbook snapshot differs")
    component = None if original is None or supplemental["supplemental_passed"] is None else bool(original and supplemental["supplemental_passed"])
    if measured["component_measurement_passed"] is not component: raise ValueError("Fulfillment component outcome differs")
    return {"data": data, "original": original, "supplemental": supplemental, "component": component, "rows": rows, "directory": directory}
