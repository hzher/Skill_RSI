"""Audit private meal workers; NeMo task activation remains a separate gate."""
import hashlib

from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_absence, track_input
from integrations.meal_plan_services import METHODS, components
from integrations.runtime_identity import BRIDGE_FILES
from integrations.task_routing import TaskRouter
from workenv.core import contained, no_links

CHECKS = ("two_owned_meal_workers", "source_bound_original_tools", "empty_original_failures", "original_recipe_detail",
    "workbook_roundtrip_and_replay", "calendar_requests_replay_without_mutation", "calendar_writes_isolated",
    "original_and_supplemental_success", "unexposed_tools_and_raw_upload_rejected", "cross_resource_and_episode_rejected",
    "close_archives_workbook_and_resets_only_its_episode", "other_worker_still_usable", "closed_identity_cannot_reseed",
    "workers_children_and_pg_closed", "shared_database_unchanged")
SOURCES = (*BRIDGE_FILES, "integrations/runtime_identity.py", "integrations/meal_worker_audit.py",
    "scripts/verify_meal_plan_workers.py", "workenv/core.py", "workenv/leases.py", "workenv/process_identity.py",
    "workenv/atomic_io.py", "training/recipe_snapshot.py", "scripts/verify_calendar_isolation.py", "scripts/diagnostic_meal_plan.py")


def audit_workers(root, path):
    receipt = read_json(path, root)
    if (receipt.get("schema_version") != "meal-plan-private-workers-diagnostic-v1" or receipt.get("passed") is not True
        or [c["name"] for c in receipt["checks"]] != list(CHECKS) or any(c["passed"] is not True for c in receipt["checks"])
        or receipt.get("execution_kind") != "private_original_meal_workers_diagnostic"
        or any(receipt.get(k) is not False for k in ("nemo_second_task_enabled", "training_eligible", "reference_solutions_read", "verified_task_bodies_read"))
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates"))):
        raise ValueError("Private meal worker scope or checks differ")
    if set(receipt["source_sha256"]) != set(SOURCES): raise ValueError("Private worker source inventory differs")
    source_binding(root, receipt["source_sha256"])
    retained = receipt["retained_sha256"]
    if not isinstance(retained, dict) or not 20 <= len(retained) <= 192: raise ValueError("Worker evidence inventory differs")
    for name, expected in retained.items():
        member = contained(root, name)
        no_links(member, root)
        if member.stat().st_size > 4 * 1024 * 1024: raise ValueError("Private worker evidence exceeds budget")
        actual = hashlib.sha256(member.read_bytes()).hexdigest()
        track_input(member, root, actual)
        if actual != expected: raise ValueError("Private worker evidence bytes changed")
    def value(name):
        if name not in retained: raise ValueError("Private worker input lacks retained byte binding")
        return read_json(contained(root, name), root)
    journal = value(receipt["journal_ref"])
    if not 35 <= len(journal) <= 100: raise ValueError("Unexpected private worker journal length")
    profiles = receipt["profiles"]
    if len(profiles) != 2 or len({p["resource_id"] for p in profiles}) != 2 or {p["slot"] for p in profiles} != {0, 1}:
        raise ValueError("Private worker profiles differ")
    _, bindings, _ = components(root)
    for name in bindings: read_json(contained(root, name), root)
    for raw in profiles:
        profile = EpisodeProfile(root, raw["resource_id"], raw["slot"])
        profile.validate_record(raw)
        def owned(name): return value((profile.base / name).relative_to(root).as_posix())
        if owned("cluster_owner.json") != raw: raise ValueError("Private worker cluster owner differs")
        ledger = owned("resources-session.json")
        rows = ledger["sessions"]
        if set(rows) != {profile.resource_id} or rows[profile.resource_id]["state"] != "closed":
            raise ValueError("Private worker ledger was not closed")
        binding = rows[profile.resource_id]["binding"]
        route = owned("meal-task-session/task_profile.json")
        if route != TaskRouter(root, "isolated").authorize(binding): raise ValueError("Private worker task profile differs")
        initial = owned("meal-task-session/initial_state.json")
        if initial["component_receipt_sha256"] != bindings or initial["calendar_events"] != 0 or initial["workbook_present"] is not False:
            raise ValueError("Private worker initial state differs")
        closed = owned("meal-task-session/services-closed.json")
        if closed["state"] != "closed" or closed["component_receipt_sha256"] != bindings:
            raise ValueError("Private worker services were not closed")
        require_dependents_closed(profile)
        for child in closed["sessions"].values():
            if child["profile"] != raw or child.get("closed") is not True: raise ValueError("Private worker child identity differs")
        track_absence(profile.data / "postmaster.pid", root)
        events = [e for e in journal if e["slot"] == profile.slot]
        tools = [e["response"]["result"] for e in events if e["route"] == "/tools" and e["response"]["status"] == 200]
        if len(tools) != 1 or {(t["server"], t["name"]) for t in tools[0]["tools"]} != {(s, n) for s, names in METHODS.items() for n in names}:
            raise ValueError("Private worker original tool inventory differs")
        if tools[0]["task_profile"] != route or {a["name"] for a in tools[0]["artifact_tools"]} != {"write_workbook", "read_workbook"}:
            raise ValueError("Private worker artifact/profile schema differs")
        verifications = [e["response"]["result"] for e in events if e["route"] == "/verify" and e["response"]["status"] == 200]
        if not verifications or verifications[0]["reward"] != 0 or verifications[0]["mask_sample"] is not False:
            raise ValueError("Private worker empty task was not a known failure")
        for measured in verifications:
            actual = value(measured["evaluation_ref"])
            if measured["evaluation"] != actual: raise ValueError("Worker verification lost its evaluator binding")
        late = [e for e in events if e["route"] == "/seed_session"][-1]
        if late["response"]["status"] != 409: raise ValueError("Closed private worker identity was recreated")
    first = [e for e in journal if e["slot"] == 0]
    successes = [e["response"]["result"] for e in first if e["route"] == "/verify" and e["response"]["status"] == 200 and e["response"]["result"]["reward"] == 1]
    if len(successes) != 1 or not all(successes[0]["evaluation"].get(k) is True for k in ("task_passed", "upstream_passed", "supplemental_passed")):
        raise ValueError("Private worker original/supplemental success differs")
    writes = [e for e in first if e["route"] == "/tool" and e["request"].get("name") == "create_event"]
    if len(writes) != 23 or [e["response"]["status"] for e in writes] != [200, 200, 409] + [200] * 20:
        raise ValueError("Calendar request replay/rebinding lineage differs")
    if writes[0]["response"]["result"] != writes[1]["response"]["result"] or writes[0]["request"] != writes[1]["request"]:
        raise ValueError("Completed request did not return the cached original result")
    if receipt["calendar_counts"] != [21, 0] or receipt["after_first_close_counts"] != [0, 0]:
        raise ValueError("Private worker calendar isolation/reset differs")
    if receipt["shared_before"] != receipt["shared_after"] or len(receipt["shared_before"]) != 6:
        raise ValueError("Private worker shared database changed")
    return {"checks": len(CHECKS), "private_workers": 2, "source_bound_profiles": 1, "calendar_events": 21,
        "nemo_second_task_enabled": False, "training_admissions": 0}
