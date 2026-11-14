"""Revalidate raw business-service evidence without starting processes or tools."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_absence, track_input
from integrations.meal_plan_services import components, METHODS, TASK_ID
from integrations.howtocook_component import NETWORK_GUARD
from workenv.core import contained

CHECKS = ("fixed_business_tool_allowlist", "opening_preserves_existing_workbook", "public_recipe_listing_matches_snapshot",
    "original_recipe_lookup", "disallowed_tool_never_forwarded", "original_invalid_arguments_keep_session_usable",
    "empty_plan_is_known_failure", "bounded_workbook_roundtrip", "original_calendar_creates_21_events",
    "both_evaluators_pass", "wrong_recipe_rejected_by_supplement", "restored_plan_passes", "both_original_children_closed",
    "instrumented_recipe_network_calls_zero", "closed_bundle_rejects_calls", "owned_postmaster_stopped", "shared_original_resources_unchanged",
    "durable_dependent_ownership_guards_slot_release")


def validate_journal(journal, recipes):
    expected = [("howtocook", "mcp_howtocook_getAllRecipes"), ("howtocook", "mcp_howtocook_getRecipeById")]
    expected += [("howtocook", "mcp_howtocook_getRecipeById")] + [("google_calendar", "create_event")] * 21 + [("google_calendar", "update_event")] * 2
    if (not isinstance(journal, list) or [(e["server"], e["name"]) for e in journal] != expected
        or any(e["response"].get("isError") for i, e in enumerate(journal) if i != 2)):
        raise ValueError("Business-service original call coverage differs")
    listing = json.loads(journal[0]["response"]["content"][0]["text"])
    detail = json.loads(journal[1]["response"]["content"][0]["text"])
    if (journal[0]["arguments"] != {} or journal[1]["arguments"] != {"query": recipes[0]["id"]}
        or listing != [{k: r[k] for k in ("name", "description") if k in r} for r in recipes] or detail != recipes[0]
        or journal[2]["arguments"] != {"query": 123} or journal[2]["response"].get("protocol_error", {}).get("code") != -32602):
        raise ValueError("Original recipe projection, lookup or protocol error differs")
    events = []
    for call in journal[3:24]:
        match = re.match(r"Event created with ID: ([0-9a-f-]+)\n", call["response"]["content"][0]["text"])
        if not match: raise ValueError("Missing original calendar creation identity")
        events.append({"id": match[1], **call["arguments"]})
    if (len({e["id"] for e in events}) != 21 or any(e["arguments"].get("eventId") != events[0]["id"] for e in journal[24:])
        or journal[24]["arguments"].get("summary") != "Cook: workagent-diagnostic-wrong-recipe"
        or journal[25]["arguments"].get("summary") != events[0]["summary"]):
        raise ValueError("Calendar mutation or restoration identity differs")
    return events


def audit_services(root, receipt_path):
    root, receipt_path = Path(root).resolve(), Path(receipt_path)
    receipt = read_json(receipt_path, root)
    source_binding(root, receipt["source_sha256"])
    if (receipt.get("schema_version") != "meal-plan-business-services-diagnostic-v1" or receipt.get("passed") is not True
        or receipt.get("execution_kind") != "scripted_original_business_services_diagnostic"
        or [c["name"] for c in receipt["checks"]] != list(CHECKS) or any(c["passed"] is not True for c in receipt["checks"])
        or any(receipt.get(k) is not False for k in ("runtime_task_activated", "nemo_second_task_enabled", "training_eligible",
            "reference_solutions_read", "verified_task_bodies_read", "os_network_sandbox"))
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates"))):
        raise ValueError("Incomplete or promoted business-service diagnostic")
    builds, bindings, _ = components(root)
    for name, checksum in bindings.items(): track_input(root / name, root, checksum)
    for key in ("recipe", "calendar", "snapshot"): source_binding(root, builds[key]["source_sha256"])
    profile = EpisodeProfile(root, receipt["profile"]["resource_id"], 0)
    profile.validate_record(receipt["profile"])
    if read_json(profile.base / "cluster_owner.json", root) != profile.record(): raise ValueError("Business service episode owner differs")
    require_dependents_closed(profile)
    dependents = read_json(profile.base / "dependent_processes.json", root)
    if dependents["children"] != receipt["services"]["sessions"]: raise ValueError("Durable business child ownership differs")
    if receipt.get("dependent_release") != {"open_blocked": True, "closed_confirmed": True}:
        raise ValueError("Missing actual dependent release observations")
    for member in ("pgdata/postmaster.pid", ".recipe.lock", ".calendar.lock", ".meal-services.lock", "recipe-process-unknown.json", "calendar-process-unknown.json"):
        track_absence(profile.base / member, root)
    retained = receipt["retained_sha256"]
    if not 24 <= len(retained) <= 40: raise ValueError("Unexpected business-service retained evidence inventory")
    for name, checksum in retained.items():
        member = contained(root, name)
        if (not member.is_relative_to(root / ".runtime/native-episodes") or member.stat().st_size > 1024 * 1024
            or hashlib.sha256(member.read_bytes()).hexdigest() != checksum): raise ValueError("Retained business-service evidence differs")
        track_input(member, root, checksum)
    record = receipt["services"]
    if (record.get("schema_version") != "meal-plan-business-services-v1" or record.get("task_id") != TASK_ID
        or record.get("profile") != profile.record() or record.get("state") != "closed" or record.get("outcome_unknown") is not False
        or type(record.get("calls")) is not int or record["calls"] != 26 or record.get("component_receipt_sha256") != bindings
        or set(record.get("sessions", {})) != set(METHODS)):
        raise ValueError("Business-service ownership, call budget or closure differs")
    summaries = [contained(root, n) for n in retained if n.endswith("/services.json")]
    if len(summaries) != 1 or read_json(summaries[0], root) != record: raise ValueError("Retained service summary differs")
    directory = summaries[0].parent
    schemas = read_json(directory / "tool_schemas.json", root)
    if len(schemas) != 7 or {(s["server"], s["name"]) for s in schemas} != {(s, n) for s, names in METHODS.items() for n in names}:
        raise ValueError("Business-service tool allowlist differs")
    for session in record["sessions"].values():
        before, after = session["before"], session["after"]
        closed = after["status"] in ("not_found", "exited") or bool(after.get("creation_id") and after["creation_id"] != before["creation_id"])
        if session.get("closed") is not True or session["profile"] != profile.record() or before["status"] != "alive" or not before.get("creation_id") or not closed:
            raise ValueError("Original business MCP child closure differs")
    recipe_record = record["sessions"]["howtocook"]
    observation = read_json(contained(root, recipe_record["observation_ref"]), root)
    guard = directory / "network_guard.mjs"
    track_input(guard, root)
    if (observation.get("pid") != recipe_record["before"]["pid"] or type(observation.get("instrumented_network_calls")) is not int
        or observation["instrumented_network_calls"] != 0 or recipe_record.get("instrumented_network_calls") != 0
        or guard.read_text(encoding="utf-8") != NETWORK_GUARD): raise ValueError("Recipe network guard observation differs")
    journal_path = receipt_path.parent / "tool_journal.json"
    journal = read_json(journal_path, root)
    if hashlib.sha256(journal_path.read_bytes()).hexdigest() != receipt["journal_sha256"]: raise ValueError("Business-service raw journal changed")
    recipes = read_json(contained(root, builds["snapshot"]["asset_ref"]), root)
    events = validate_journal(journal, recipes)
    from adapters.meal_plan_evaluation import classify_original
    from adapters.meal_plan_validation import measure_workbook
    from training.task_lifecycle_design import pinned_sources, MEMBERS
    source = read_json(root / "configs/mainline_sources.lock.json", root)["sources"]["toolathlon_gym"]
    originals, identities = pinned_sources(root, source, members={"evaluation": MEMBERS["evaluation"]})
    stages = {"empty": (False, False, False), "positive": (True, True, True), "wrong_recipe": (False, True, False), "restored": (True, True, True)}
    if set(receipt["evaluations"]) != set(stages): raise ValueError("Business-service evaluation coverage differs")
    for stage, expected in stages.items():
        evaluation_path = contained(root, receipt["evaluations"][stage]["ref"])
        measured = read_json(evaluation_path, root)
        source_binding(root, measured["source_sha256"])
        if (tuple(measured[k] for k in ("task_passed", "upstream_passed", "supplemental_passed")) != expected
            or measured["profile"] != profile.record() or measured["reward"] is not None or measured["evaluation_status"] != "completed"
            or measured["original_evaluator_executed"] is not True or measured["original_evaluator_source"] != identities["evaluation"]
            or (evaluation_path.parent / "original_evaluation.py").read_bytes() != originals["evaluation"]
            or classify_original(measured["upstream_exit_code"], (evaluation_path.parent / "original.stdout.txt").read_text(encoding="utf-8"),
                (evaluation_path.parent / "original.stderr.txt").read_text(encoding="utf-8")) != expected[1]):
            raise ValueError("Original business-service evaluation identity or result differs")
    artifact = receipt["artifact_write"]
    path = contained(root, artifact["artifact_ref"])
    if (path != profile.base / "calendar-workspace/Weekly_Meal_Plan.xlsx" or artifact["profile"] != profile.record()
        or artifact["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest() or artifact["bytes"] != path.stat().st_size
        or measure_workbook(events, path.parent, recipes)["task_passed"] is not True): raise ValueError("Business-service workbook differs")
    intents = [read_json(contained(root, n), root) for n in retained if "/meal-artifact-write-" in n]
    if len(intents) != 2 or {**artifact, "state": "completed"} not in intents: raise ValueError("Business-service artifact intents differ")
    corrupted = deepcopy(events)
    corrupted[0]["summary"] = journal[24]["arguments"]["summary"]
    if measure_workbook(corrupted, path.parent, recipes)["task_passed"] is not False: raise ValueError("Wrong recipe no longer fails the supplemental verifier")
    if receipt["shared_before"] != receipt["shared_after"] or len(receipt["shared_before"]) != 6: raise ValueError("Shared original resources changed")
    return {"checks": len(CHECKS), "original_tool_calls": 26, "original_evaluations": 4, "nemo_second_task_enabled": False}
