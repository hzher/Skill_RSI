"""Recompute shipping diagnostics and full original/project read inventories."""
import hashlib
import json

from adapters import shipping_validation as validation
from adapters.shipping_evaluation import classify_original, REFERENCE_MODE, profile_registered
from adapters.office_workbook import read_tables
from integrations.customer_report_audit import SOURCES as OFFICE_SOURCES
from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_absence, track_input
from integrations.shipping_components import check_original_sources, METHODS
from training.office_task_review import TASKS
from workenv.core import contained

CHECKS = ("component_run_without_nemo_binding", "two_owned_empty_gsheet_resources", "nine_allowlisted_methods_with_project_read_extension",
    "two_missing_workbook_failures", "complete_orders_zones_locations_and_method_instances", "independent_sql_python_and_original_parser_success",
    "transaction_workbook_roundtrip", "cloud_cent_error_rejected", "workbook_money_tolerance_rejected", "restored_component_success",
    "zero_shipping_cost_omission_rejected", "unallowlisted_mutation_refused", "foreign_task_marker_unknown", "unresolved_write_blocks_measurement",
    "reset_one_preserves_other", "readonly_sources_unchanged", "default_original_interface_has_no_project_extension", "children_and_postgres_closed", "shared_database_unchanged")
SOURCES = tuple(dict.fromkeys((*OFFICE_SOURCES, "integrations/shipping_components.py", "integrations/shipping_audit.py",
    "adapters/shipping_evaluation.py", "adapters/shipping_validation.py", "scripts/verify_shipping_components.py", "training/shipping_task_review.py")))


def audit_components(root, path):
    receipt = read_json(path, root)
    if (receipt.get("schema_version") != "shipping-original-components-diagnostic-v1" or receipt.get("passed") is not True
        or [c["name"] for c in receipt["checks"]] != list(CHECKS) or any(c["passed"] is not True for c in receipt["checks"])
        or receipt.get("reference_mode") != REFERENCE_MODE or receipt.get("task_passed") is not None or receipt.get("reward") is not None
        or receipt.get("runtime_profile_registered") is not profile_registered(root)
        or any(receipt.get(k) is not False for k in ("nemo_task_executed", "upstream_reference_used", "official_benchmark_score_available", "training_eligible"))
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates"))):
        raise ValueError("Shipping component scope differs")
    if set(receipt["source_sha256"]) != set(SOURCES): raise ValueError("Shipping source inventory differs")
    source_binding(root, receipt["source_sha256"])
    retained = receipt["retained_sha256"]
    if not isinstance(retained, dict) or not 20 <= len(retained) <= 512: raise ValueError("Shipping retained inventory differs")
    for name, expected in retained.items():
        member = contained(root, name)
        if member.stat().st_size > 16 * 1024 * 1024: raise ValueError("Shipping evidence member budget")
        digest = hashlib.sha256(member.read_bytes()).hexdigest()
        if digest != expected: raise ValueError("Shipping raw evidence bytes differ")
        track_input(member, root, digest)
    count, cloud_blind, money_blind, cost_blind = 0, False, False, False
    truth_by_index = {}
    for row in receipt["measurements"]:
        measured = row["result"]; ref = measured["evaluation_ref"]
        if ref not in retained or read_json(contained(root, ref), root) != measured: raise ValueError("Shipping measurement summary changed")
        checked = audit_measurement(root, measured)
        data, original, supplemental, truth, directory = (checked[k] for k in ("data", "original", "supplemental", "truth", "directory"))
        truth_by_index.setdefault(row["index"], data)
        if data["readonly_sha256"] != truth_by_index[row["index"]]["readonly_sha256"]: raise ValueError("Shipping read-only source changed")
        if original is True and supplemental["supplemental_passed"] is False:
            local = read_tables(directory, "Shipping_Analysis.xlsx", validation.HEADERS)
            if validation.canonical(local["Zone Summary"]) != validation.canonical(truth["rows"]): money_blind = True
            else:
                try: validation.check_methods(local["Methods"], truth["methods"])
                except validation.InvalidReport: cost_blind = True
                else: cloud_blind = True
        count += 1
    if count != 12 or not all((cloud_blind, money_blind, cost_blind)) or receipt["shared_before"] != receipt["shared_after"]:
        raise ValueError("Shipping component cases incomplete")
    if len(receipt["profiles"]) != 2: raise ValueError("Shipping requires two owned resource profiles")
    for i, record in enumerate(receipt["profiles"]):
        profile = EpisodeProfile(root, record["resource_id"], record["slot"]); profile.validate_record(record)
        require_dependents_closed(profile); track_absence(profile.data / "postmaster.pid", root)
        run = profile.base / "shipping-components-run"
        schemas = read_json(run / "tool_schemas.json", root)
        if {(s["server"], s["name"]) for s in schemas} != {(server, name) for server, names in METHODS.items() for name in names}:
            raise ValueError("Shipping tool schema inventory differs")
        journal = read_json(run / "tool_journal.json", root)
        def calls(method): return [e for e in journal if e["server"] == "woocommerce" and e["name"] == method]
        def decode(event): return json.loads("".join(b["text"] for b in event["response"]["content"] if b["type"] == "text"))
        pages = calls("woo_orders_list")
        if not pages or [e["arguments"]["page"] for e in pages] != list(range(1, len(pages)+1)) or len(decode(pages[-1])) >= 10:
            raise ValueError("Shipping order pagination termination differs")
        zones = calls("woo_shipping_zones_list")
        if len(zones) != 1: raise ValueError("Shipping zone calls differ")
        actual = {"orders": [r for event in pages for r in decode(event)], "shipping_zones": decode(zones[0])}
        for method, table in (("woo_shipping_zone_methods_list", "shipping_zone_methods"), ("workenv_shipping_zone_locations_get", "shipping_zone_locations")):
            events = calls(method)
            if sorted(e["arguments"]["zoneId"] for e in events) != sorted(z["id"] for z in actual["shipping_zones"]):
                raise ValueError("Shipping per-zone read inventory differs")
            actual[table] = [r for event in events for r in decode(event)]
        check_original_sources(truth_by_index[i], actual)
    first = EpisodeProfile(root, receipt["profiles"][0]["resource_id"], receipt["profiles"][0]["slot"])
    if "workenv_shipping_zone_locations_get" in read_json(first.base / "shipping-components-run/default-woocommerce-tools.json", root):
        raise ValueError("Shipping extension leaked into default original interface")
    return {"checks": len(CHECKS), "measurements_recomputed": count, "reference_mode": REFERENCE_MODE,
            "nemo_task_executed": False, "runtime_profiles_added": 0, "official_score_available": False}


def audit_measurement(root, measured):
    ref = measured["evaluation_ref"]
    if read_json(contained(root, ref), root) != measured: raise ValueError("Shipping measurement changed")
    directory = contained(root, ref).parent
    if hashlib.sha256((directory / "original_evaluation.py").read_bytes()).hexdigest() != TASKS[validation.TASK_ID]["evaluation/main.py"]:
        raise ValueError("Shipping evaluator is not the pinned original")
    data = read_json(contained(root, measured["snapshot_ref"]), root)
    supplemental = validation.measure_workbook(data, directory)
    original = classify_original(measured["original_exit_code"], (directory / "original.stdout.txt").read_text(), (directory / "original.stderr.txt").read_text())
    if (original != measured["original_parser_passed"] or supplemental != measured["supplemental"]
        or measured["supplemental_passed"] is not supplemental["supplemental_passed"] or measured["reference_mode"] != REFERENCE_MODE
        or measured.get("runtime_profile_registered") is not profile_registered(root)
        or any(measured.get(k) is not False for k in ("upstream_reference_used", "official_benchmark_score_available", "training_eligible"))
        or any(measured.get(k) is not True for k in ("sql_python_aggregate_agree", "source_completeness_verified"))
        or measured["task_passed"] is not None or measured["reward"] is not None):
        raise ValueError("Shipping measurement recomputation differs")
    truth = validation.expected(data["orders"], data["shipping_zones"], data["shipping_zone_locations"], data["shipping_zone_methods"], source_binding=data["source_binding"])
    sql = read_json(directory / "sql_truth.json", root)
    if (set(sql) != {"rows", "methods"} or validation.canonical(sql["rows"]) != validation.canonical(truth["rows"])
        or sql["methods"] != truth["methods"]):
        raise ValueError("Shipping independent SQL truth differs")
    oracle = directory / "project-derived-oracle/Shipping_Analysis.xlsx"
    if hashlib.sha256(oracle.read_bytes()).hexdigest() != measured["oracle_sha256"]: raise ValueError("Shipping oracle bytes changed")
    tables = read_tables(oracle.parent, oracle.name, validation.HEADERS)
    if validation.canonical(tables["Zone Summary"]) != validation.canonical(truth["rows"]): raise ValueError("Shipping oracle zone rows differ")
    validation.check_methods(tables["Methods"], truth["methods"])
    workbook = directory / oracle.name
    if (hashlib.sha256(workbook.read_bytes()).hexdigest() if workbook.exists() else None) != measured["workbook_sha256"]:
        raise ValueError("Shipping workbook snapshot differs")
    component = None if original is None or supplemental["supplemental_passed"] is None else bool(original and supplemental["supplemental_passed"])
    if measured["component_measurement_passed"] is not component: raise ValueError("Shipping component outcome differs")
    return {"data": data, "original": original, "supplemental": supplemental, "component": component, "truth": truth, "directory": directory}
