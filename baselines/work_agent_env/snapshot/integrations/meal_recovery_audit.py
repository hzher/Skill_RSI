"""Recheck raw original-MCP fault/reconciliation evidence without replaying it."""
import hashlib

from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_absence, track_input
from integrations.meal_plan_services import components
from workenv.core import contained, no_links

CHECKS = (
    "original_bundle_two_live_children", "active_controller_blocks_reconciliation", "active_dependents_block_pg_recovery",
    "owned_controller_fault_confirmed", "original_children_closed_identity_attested", "unreconciled_children_block_pg_recovery",
    "stale_component_locks_require_explicit_option", "dependents_archived_without_slot_change", "reconciliation_is_idempotent",
    "original_mutation_and_artifact_unchanged_no_replay", "orphan_pg_recovered_after_dependents_closed", "shared_database_unchanged")
SOURCES = ("scripts/verify_meal_plan_recovery.py", "integrations/meal_recovery_audit.py", "integrations/dependent_reconciliation.py",
    "integrations/meal_plan_services.py", "integrations/resource_transport.py", "integrations/episode_cluster.py", "integrations/calendar_component.py",
    "integrations/howtocook_component.py", "adapters/meal_plan_artifacts.py", "adapters/meal_plan_evaluation.py",
    "adapters/meal_plan_validation.py", "workenv/leases.py", "workenv/process_identity.py",
    "workenv/core.py", "workenv/atomic_io.py", "workenv/attested_process.py",
    "scripts/verify_calendar_isolation.py", "training/recipe_snapshot.py")


def audit_recovery(root, path):
    receipt = read_json(path, root)
    if (receipt.get("schema_version") != "meal-plan-dependent-recovery-diagnostic-v1" or receipt.get("passed") is not True
        or receipt.get("execution_kind") != "owned_original_mcp_controller_fault_diagnostic" or receipt.get("cleanup_errors") != []
        or [c["name"] for c in receipt["checks"]] != list(CHECKS) or any(c["passed"] is not True for c in receipt["checks"])
        or any(receipt.get(k) is not False for k in ("nemo_second_task_enabled", "training_eligible", "reference_solutions_read", "verified_task_bodies_read"))
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates"))):
        raise ValueError("Original MCP recovery scope or check inventory differs")
    if set(receipt["source_sha256"]) != set(SOURCES): raise ValueError("Recovery source inventory differs")
    source_binding(root, receipt["source_sha256"])
    retained = receipt["retained_sha256"]
    if not isinstance(retained, dict) or not 12 <= len(retained) <= 128: raise ValueError("Recovery retained inventory differs")
    for name, expected in retained.items():
        member = contained(root, name)
        no_links(member, root)
        if member.stat().st_size > 4 * 1024 * 1024: raise ValueError("Recovery member exceeds budget")
        actual = hashlib.sha256(member.read_bytes()).hexdigest()
        track_input(member, root, actual)
        if actual != expected: raise ValueError("Recovery evidence bytes changed")
    def retained_path(name):
        if name not in retained: raise ValueError("Recovery member lacks retained byte binding")
        return contained(root, name)
    def value(name): return read_json(retained_path(name), root)
    proof = value(receipt["observations_ref"])
    raw_profile = receipt["profile"]
    profile = EpisodeProfile(root, raw_profile["resource_id"], raw_profile["slot"])
    profile.validate_record(raw_profile)
    if read_json(profile.base / "cluster_owner.json", root) != raw_profile: raise ValueError("Recovery episode owner differs")
    ready = value(proof["ready_ref"])
    if ready["profile"] != raw_profile or ready["services"]["profile"] != raw_profile: raise ValueError("Recovery ready binding differs")
    _, bindings, _ = components(root)
    if ready["services"]["component_receipt_sha256"] != bindings: raise ValueError("Recovery component build changed")
    for name in bindings: read_json(contained(root, name), root)
    before = ready["controller"]
    after = proof["controller_after"]
    if (before["status"] != "alive" or not before.get("creation_id") or after["pid"] != before["pid"]
        or after["status"] not in ("not_found", "exited")):
        raise ValueError("Recovery controller exit identity differs")
    original = value(proof["originals"]["dependents"])
    sessions = ready["services"]["sessions"]
    if (original["profile"] != raw_profile or original["state"] != "open" or original["children"] != sessions
        or set(sessions) != {"howtocook", "google_calendar"} or ready["services"]["calls"] != 2):
        raise ValueError("Recovery original dependent inventory differs")
    live = False
    for name, session in sessions.items():
        prior = session["before"]
        observed = proof["children_after_controller_fault"][name]
        final = proof["children_after"][name]
        if (session["profile"] != raw_profile or prior["status"] != "alive" or not prior.get("creation_id")
            or observed["pid"] != prior["pid"] or final["pid"] != prior["pid"] or final["status"] not in ("not_found", "exited")):
            raise ValueError("Recovery original child identity differs")
        live |= observed["status"] == "alive" and observed.get("creation_id") == prior["creation_id"]
    if proof["live_child_reconciliation_refused"] is not (True if live else None): raise ValueError("Live-child refusal observation differs")
    if not set(proof["diagnostic_child_terminations"]) <= set(sessions): raise ValueError("Diagnostic terminated an undeclared child")
    require_dependents_closed(profile)
    current = read_json(profile.base / "dependent_processes.json", root)
    reconciled = proof["reconciliation"]
    original_raw = retained_path(proof["originals"]["dependents"]).read_bytes()
    if (current["reconciliation"]["original_sha256"] != hashlib.sha256(original_raw).hexdigest()
        or current["reconciliation"]["archives"] != reconciled["archives"] or reconciled["outcome"] != "children_confirmed_closed"
        or proof["idempotent"]["outcome"] != "already_resolved"):
        raise ValueError("Recovery reconciliation outcome differs")
    archive_bytes = [retained_path(name).read_bytes() for name in reconciled["archives"]]
    if original_raw not in archive_bytes: raise ValueError("Original dependent record was not archived byte for byte")
    if set(proof["originals"]["component_locks"]) != {".meal-services.lock", ".recipe.lock", ".calendar.lock"}:
        raise ValueError("Meal recovery component lease inventory differs")
    for name, ref in proof["originals"]["component_locks"].items():
        lock = value(ref)
        if lock["process"] != before or retained_path(ref).read_bytes() not in archive_bytes:
            raise ValueError("Component lease archive has another controller identity")
        track_absence(profile.base / name, root)
    for outcome in (reconciled, proof["idempotent"]):
        if outcome["processes_terminated"] != 0 or outcome["requests_replayed"] != 0 or outcome["slot_released"] is not False:
            raise ValueError("Coordinator claims process termination, replay or slot release")
    slot_before, slot_after = value(proof["originals"]["slot"]), value(proof["slot_after_ref"])
    if slot_before["state"] != "reserved" or slot_after != {**slot_before, "state": "stopped"} or slot_before["resource_id"] != profile.resource_id:
        raise ValueError("PG recovery slot lineage differs")
    lease = value(proof["originals"]["lease"])
    if lease["process"] != before or retained_path(proof["pg_recovery"]["lease_archive_ref"]).read_bytes() != retained_path(proof["originals"]["lease"]).read_bytes():
        raise ValueError("Native lease archive differs")
    track_absence(profile.data / "postmaster.pid", root)
    journal = ready["journal"]
    if (len(journal) != 2 or [(c["server"], c["name"]) for c in journal] != [("howtocook", "mcp_howtocook_getRecipeById"), ("google_calendar", "create_event")]
        or journal[-1]["response"].get("isError") or len(ready["events"]) != 1 or ready["events"] != proof["events_after_reconciliation"]
        or ready["events"][0]["summary"] != journal[-1]["arguments"]["summary"]):
        raise ValueError("Original calendar mutation was changed or replayed")
    workbook = retained_path(ready["workbook_ref"])
    if hashlib.sha256(workbook.read_bytes()).hexdigest() != ready["artifact"]["sha256"]: raise ValueError("Recovery workbook changed")
    if proof["shared_before"] != proof["shared_after"] or len(proof["shared_before"]) != 6: raise ValueError("Shared database differs")
    return {"checks": len(CHECKS), "original_children": len(sessions), "live_child_refusal_observed": live,
        "diagnostic_child_terminations": len(proof["diagnostic_child_terminations"]), "coordinator_processes_terminated": 0,
        "requests_replayed": 0, "nemo_second_task_enabled": False}
