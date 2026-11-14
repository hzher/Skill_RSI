"""Recompute original inventory tool effects and component measurements."""
import hashlib
from uuid import UUID

from integrations import inventory_components as component
from integrations import inventory_data as data
from integrations.email_audit import audit_journal as audit_mail
from integrations.email_build import BUILD_INPUTS
from integrations.calendar_component import BUILD_INPUTS as CALENDAR_INPUTS
from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.inventory_evaluation_audit import audit_measurement
from integrations.mainline_audit import read_json, source_binding, track_input, track_absence
from workenv.core import contained, ResourceError

CHECKS = ("two_owned_three_service_resources", "original_products_complete_and_sql_equal", "two_real_business_outputs_pass",
    "same_request_no_duplicate_events_or_mail", "conflict_and_foreign_event_refused", "wrong_report_supplement_rejects",
    "wrong_calendar_supplement_rejects", "restored_business_passes", "partial_send_unknown_and_retained",
    "unknown_blocks_requests_measurement_and_reset", "closing_and_reset_first_preserves_second",
    "six_children_and_two_pg_closed", "shared_database_and_nemo_ledgers_unchanged")
SOURCES = tuple(dict.fromkeys(("integrations/inventory_components.py", "integrations/inventory_components_audit.py",
    "scripts/verify_inventory_components.py", "tests/test_inventory_components.py", "integrations/inventory_data.py",
    "adapters/inventory_alert_evaluation.py", "integrations/inventory_evaluation_audit.py", "integrations/inventory_data_audit.py",
    "integrations/order_analysis_resource.py", "adapters/woocommerce_native.mjs", "scripts/prepare_inventory_data_seed.py",
    "integrations/email_audit.py", "integrations/email_components.py", *BUILD_INPUTS, *CALENDAR_INPUTS)))


def audit_requests(root, profile, retained):
    run = profile.base / "inventory-components-run"
    journal = read_json(run / "requests.json", root, limit=32 * 1024 * 1024)
    if (journal.get("schema_version") != component.JOURNAL_SCHEMA or journal.get("profile") != profile.record()
        or journal.get("task_id") != data.TASK_ID or type(journal.get("entries")) is not dict
        or not 1 <= len(journal["entries"]) <= 128): raise ValueError("Inventory request journal identity differs")
    source = run / "initial_snapshot.json"
    nested = read_json(profile.base / "email-components-run/requests.json", root, limit=32 * 1024 * 1024)
    known, unknown, creates, sends = 0, 0, 0, 0
    for rid, entry in journal["entries"].items():
        if str(UUID(rid)) != rid: raise ValueError("Inventory request UUID differs")
        server, name, args = entry["server"], entry["name"], entry["arguments"]
        component.validate_call(server, name, args)
        if entry["fingerprint"] != component.fingerprint(server + "/" + name, args): raise ValueError("Inventory request fingerprint differs")
        snapshots = []
        for phase in ("before", "after"):
            path = run / (rid + "." + phase + ".json")
            if entry.get(phase + "_ref") != path.relative_to(root).as_posix() or path.relative_to(root).as_posix() not in retained:
                raise ValueError("Inventory snapshot path rebound or missing")
            track_input(path, root); snapshots.append(component.load_snapshot(root, path, source))
        before, after = snapshots
        if entry["state"] == "committed":
            component.verify_effect(server, name, args, entry["response"], before, after)
            if server == "email" and (nested["entries"][rid]["state"] != "committed" or nested["entries"][rid]["response"] != entry["response"]):
                raise ValueError("Inventory outer and email request results differ")
            known += 1; creates += int(name == "create_event"); sends += int(name == "send_email")
        elif entry["state"] == "unknown":
            mail = nested["entries"].get(rid, {})
            if (server != "email" or name != "send_email" or mail.get("state") != "unknown"
                or mail.get("arguments") != args or entry.get("error_type") != "ResourceError"
                or before["email"]["sent_log"] != after["email"]["sent_log"]
                or len(after["email"]["messages"]) != len(before["email"]["messages"]) + 1):
                raise ValueError("Inventory unknown is not a retained original partial send")
            try: component.verify_effect(server, name, args, mail["response"], before, after)
            except ResourceError: pass
            else: raise ValueError("Inventory failed call relabelled successful")
            unknown += 1
        else: raise ValueError("Inventory diagnostic left an unobserved request")
    return {"committed": known, "unknown": unknown, "creates": creates, "sends": sends}


def audit(root, path):
    receipt = read_json(path, root)
    if (receipt.get("schema_version") != "inventory-original-components-v1" or receipt.get("passed") is not True
        or [r.get("name") for r in receipt.get("checks", [])] != list(CHECKS)
        or any(r.get("passed") is not True for r in receipt["checks"])
        or set(receipt.get("source_sha256", {})) != set(SOURCES)
        or receipt.get("fixture_mode") != "original_woocommerce_calendar_pg_email_tools"
        or receipt.get("task_passed") is not None or receipt.get("reward") is not None
        or any(receipt.get(k) is not False for k in ("nemo_task_executed", "runtime_activation", "training_admission", "reference_files_read", "official_benchmark_score_available", "hard_interruption_verified"))
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates", "external_messages_sent"))):
        raise ValueError("Inventory component receipt scope or coverage differs")
    source_binding(root, receipt["source_sha256"])
    from integrations.email_build import verify_build as verify_email
    from integrations.calendar_component import verify_build as verify_calendar
    from integrations.email_task_identity import _build_digest
    if receipt["email_build"] != read_json(root / "evidence/mainline/email_build.json", root) or receipt["calendar_build"] != read_json(root / "evidence/mainline/calendar_build.json", root):
        raise ValueError("Inventory component build receipt rebound")
    verify_email(root, receipt["email_build"], check_environment=True); verify_calendar(root, receipt["calendar_build"])
    profiles = [EpisodeProfile(root, r["resource_id"], r["slot"]) for r in receipt["profiles"]]
    if len(profiles) != 2 or [p.slot for p in profiles] != [0, 1] or profiles[0].resource_id == profiles[1].resource_id:
        raise ValueError("Inventory two-slot identity differs")
    retained = receipt.get("retained_sha256", {})
    if not 32 <= len(retained) <= 512: raise ValueError("Inventory retained inventory budget")
    for name, sha in retained.items():
        member = contained(root, name)
        if (not any(member.is_relative_to(p.base) for p in profiles) and not member.is_relative_to(path.parent)
            or "pgdata" in member.parts or member.stat().st_size > 8 * 1024 * 1024
            or hashlib.sha256(member.read_bytes()).hexdigest() != sha): raise ValueError("Inventory retained bytes or owner differs")
        track_input(member, root, sha)
    request_counts = []
    for i, (profile, raw_profile) in enumerate(zip(profiles, receipt["profiles"])):
        profile.validate_record(raw_profile)
        run = profile.base / "inventory-components-run"
        initial = read_json(run / "initial_snapshot.json", root, limit=data.MAX_SNAPSHOT_BYTES)
        data.validate_snapshot(initial)
        if initial["events"] or any(initial["email"].values()): raise ValueError("Inventory did not initialize empty")
        pages = read_json(run / "original-pages.json", root, limit=4 * 1024 * 1024)
        data.audit_pages(pages, initial["products"])
        schemas = read_json(run / "tool_schemas.json", root)
        if len(schemas) != 7 or {(s["server"], s["name"]) for s in schemas} != {(k, n) for k, names in component.METHODS.items() for n in names}:
            raise ValueError("Inventory original method inventory differs")
        requests = audit_requests(root, profile, retained)
        if requests != {"committed": 17, "unknown": i, "creates": 5, "sends": 1}:
            raise ValueError("Inventory tool request coverage differs")
        request_counts.append(requests)
        known_mail, unknown_mail = audit_mail(root, profile, retained, task_id=data.TASK_ID)
        if (len(known_mail), len(unknown_mail)) != (1, i): raise ValueError("Inventory nested email coverage differs")
        observed = read_json(profile.base / "email-components-run/email-process.json", root)
        launch = read_json(profile.base / "email-components-run/launch.json", root)
        binding = launch["task_binding"]
        marker = read_json(profile.base / "email-components-run/initialization.json", root)
        if (launch.get("build") != receipt["email_build"] or observed.get("build_fingerprint") != receipt["email_build"]["build_fingerprint"]
            or launch.get("schema_version") != "owned-email-task-launch-v1" or launch.get("profile") != profile.record()
            or binding.get("build_sha256") != _build_digest(receipt["email_build"])
            or observed.get("task_binding") != binding or binding.get("task_id") != data.TASK_ID
            or binding.get("sender") != data.validation.SENDER or binding.get("profile") != profile.record()
            or binding.get("marker_sha256") != hashlib.sha256((profile.base / "email-components-run/initialization.json").read_bytes()).hexdigest()
            or marker.get("preprocess_source") != data.public_blob(root, "preprocess/main.py")[1]
            or marker.get("profile") != profile.record() or marker.get("task_id") != data.TASK_ID
            or marker.get("contract_source") != data.validation.source_identity()
            or binding.get("preprocess_source") != marker.get("preprocess_source")
            or binding.get("readonly_sha256") != initial["readonly_sha256"]
            or observed.get("profile") != profile.record() or observed.get("methods") != list(component.METHODS["email"])
            or observed.get("pg_data_ref") != profile.data.relative_to(root).as_posix()
            or observed.get("forbidden_socket_blocked") is not True or observed.get("external_messages_sent") != 0
            or observed.get("backend_classes") != ["emails_mcp.backends.pg_backend.PgIMAPBackend", "emails_mcp.backends.pg_backend.PgSMTPBackend"]):
            raise ValueError("Inventory original mail binding differs")
        dependencies = read_json(profile.base / "dependent_processes.json", root)
        require_dependents_closed(profile); track_absence(profile.data / "postmaster.pid", root)
        if set(dependencies["children"]) != set(component.METHODS) or dependencies["children"]["email"]["before"] != observed["process"]:
            raise ValueError("Inventory three-child closure differs")
        wc = read_json(run / "woocommerce-session.json", root)
        calendars = list(profile.base.glob("calendar-process-*.session.json"))
        if wc != dependencies["children"]["woocommerce"] or len(calendars) != 1 or read_json(calendars[0], root) != dependencies["children"]["google_calendar"]:
            raise ValueError("Inventory original child session evidence differs")
        calendar_marker = read_json(profile.base / "calendar-initialized.json", root)
        if (calendar_marker.get("task_id") != data.TASK_ID or calendar_marker.get("inventory_marker_sha256") != binding["marker_sha256"]
            or calendar_marker.get("profile") != profile.record() or calendar_marker.get("build_fingerprint") != receipt["calendar_build"]["build_fingerprint"]):
            raise ValueError("Inventory Calendar service binding differs")
    results = []
    for record in receipt["measurements"]:
        i = record["slot"]
        if type(i) is not int or i not in (0, 1): raise ValueError("Inventory measurement slot differs")
        result = audit_measurement(root, contained(root, record["evaluation_ref"]), profiles[i])
        results.append((record["name"], i, result["original_passed"], result["component_measurement_passed"]))
    if results != [("positive", 0, True, True), ("positive", 1, True, True), ("wrong_report", 0, True, False),
                   ("wrong_calendar", 0, True, False), ("restored", 0, True, True)]:
        raise ValueError("Inventory real-tool measurement conclusions differ")
    reset = read_json(path.parent / "reset_snapshots.json", root, limit=16 * 1024 * 1024)
    if reset["first"]["events"] or any(reset["first"]["email"][n] for n in data.RESET_TABLES) or reset["second_before"] != reset["second_after"]:
        raise ValueError("Inventory reset crossed resources")
    initial = read_json(profiles[0].base / "inventory-components-run/initial_snapshot.json", root, limit=data.MAX_SNAPSHOT_BYTES)
    if any(reset["first"][k] != initial[k] for k in component.IMMUTABLE): raise ValueError("Inventory reset changed read-only source")
    report = profiles[0].base / "inventory-workspace" / data.validation.FILENAME
    data.validation.check_report(report.read_text(encoding="utf-8"), data.validate_snapshot(initial))
    if report.relative_to(root).as_posix() not in retained: raise ValueError("Inventory preserved report missing")
    if receipt["shared_before"] != receipt["shared_after"] or receipt["ledgers_before"] != receipt["ledgers_after"]:
        raise ValueError("Inventory components changed shared source or NeMo ledgers")
    if set(receipt["ledgers_before"]) != {"resource-sessions.json", "isolated-sessions.json", "synthetic-sessions.json"}:
        raise ValueError("Inventory ledger inventory differs")
    for name, sha in receipt["ledgers_before"].items():
        if name not in ("resource-sessions.json", "isolated-sessions.json", "synthetic-sessions.json"): raise ValueError("Inventory ledger name differs")
        for phase in ("before", "after"):
            member = path.parent / ("ledger-" + phase + "-" + name)
            if member.relative_to(root).as_posix() not in retained or hashlib.sha256(member.read_bytes()).hexdigest() != sha:
                raise ValueError("Inventory retained ledger bytes differ")
    return {"checks": len(CHECKS), "actual_original_tool_requests": sum(r["committed"] + r["unknown"] for r in request_counts),
        "original_product_pages": 18, "original_calendar_creates": 10, "original_mail_sends": 2, "partial_mail_sends": 1,
        "measurements_recomputed": len(results), "new_runtime_profiles": 0, "hard_interruption_verified": False, "external_messages_sent": 0}
