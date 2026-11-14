"""Raw NeMo evidence for the second original task, separate from training."""
import hashlib

from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_absence, track_input
from integrations.meal_plan_services import METHODS, components
from integrations.runtime_identity import expected_identity
from integrations.task_routing import TaskRouter
from workenv.core import contained, no_links

CHECKS = ("unknown_and_shared_task_rejected_before_allocation", "two_nemo_meal_sessions", "source_bound_original_mcp_schemas",
    "cookie_and_episode_binding", "two_known_empty_failures", "original_recipe_lookup", "bounded_workbook_roundtrip",
    "calendar_mutations_are_idempotent_and_isolated", "original_and_supplemental_success", "supplement_rejects_wrong_recipe",
    "restored_plan_passes", "infrastructure_fault_masks_reward", "identity_restore_preserves_success",
    "unexposed_tools_rejected", "close_preserves_other_episode", "closed_identity_and_unknown_masking",
    "all_workers_and_children_closed", "other_ledgers_and_shared_database_unchanged")
EXTRA_SOURCES = ("scripts/verify_nemo_meal_plan.py", "scripts/diagnostic_meal_plan.py", "integrations/nemo_meal_audit.py",
    "integrations/nemo_http.py", "scripts/prepare_episode_seed.py", "scripts/verify_calendar_isolation.py")


def audit_nemo_meal(root, path):
    receipt = read_json(path, root)
    if (receipt.get("schema_version") != "nemo-original-meal-plan-diagnostic-v1" or receipt.get("passed") is not True
        or receipt.get("framework") != "nemo_gym" or receipt.get("execution_kind") != "scripted_original_task_diagnostic"
        or receipt.get("nemo_second_task_enabled") is not True or receipt.get("training_eligible") is not False
        or [c["name"] for c in receipt["checks"]] != list(CHECKS) or any(c["passed"] is not True for c in receipt["checks"])
        or any(receipt.get(k) != 0 for k in ("real_model_calls", "training_updates", "training_admissions"))
        or receipt.get("reference_solutions_read") is not False or receipt.get("verified_task_bodies_read") is not False):
        raise ValueError("NeMo meal-plan diagnostic scope differs")
    expected = {**expected_identity(root), **{name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in EXTRA_SOURCES}}
    if receipt["source_sha256"] != expected: raise ValueError("NeMo meal-plan source inventory or bytes differ")
    source_binding(root, receipt["source_sha256"])
    retained = receipt["retained_sha256"]
    for name, digest in retained.items():
        member = contained(root, name)
        no_links(member, root)
        if member.stat().st_size > 4 * 1024 * 1024: raise ValueError("NeMo meal evidence exceeds budget")
        actual = hashlib.sha256(member.read_bytes()).hexdigest()
        track_input(member, root, actual)
        if actual != digest: raise ValueError("NeMo meal-plan evidence changed")
    def value(name):
        if name not in retained: raise ValueError("NeMo meal input lacks a retained binding")
        return read_json(contained(root, name), root)
    journal = value(receipt["journal_ref"])
    _, bindings, _ = components(root)
    for name in bindings: read_json(contained(root, name), root)
    seeds = [e["request"] for e in journal if e["route"] == "/isolated/seed_session" and e["status"] == 200]
    if len(seeds) != 2 or len({s["resources_session_id"] for s in seeds}) != 2: raise ValueError("Two distinct NeMo meal sessions required")
    for seed in seeds:
        base = root / ".runtime/native-episodes" / seed["resources_session_id"]
        owner = value((base / "cluster_owner.json").relative_to(root).as_posix())
        profile = EpisodeProfile(root, seed["resources_session_id"], owner["slot"])
        profile.validate_record(owner)
        route = value((base / "meal-task-session/task_profile.json").relative_to(root).as_posix())
        if route != TaskRouter(root, "isolated").authorize(seed): raise ValueError("NeMo meal task profile differs")
        initial = value((base / "meal-task-session/initial_state.json").relative_to(root).as_posix())
        if initial["component_receipt_sha256"] != bindings: raise ValueError("NeMo meal component binding differs")
        ledger = value((base / "resources-session.json").relative_to(root).as_posix())
        if ledger["sessions"][profile.resource_id]["state"] != "closed": raise ValueError("NeMo meal worker was not closed")
        require_dependents_closed(profile)
        read_json(base / "dependent_processes.json", root)
        track_absence(profile.data / "postmaster.pid", root)
    schema = [e["response"] for e in journal if e["route"] == "/isolated/tools" and e["status"] == 200]
    if len(schema) != 2 or any({(t["server"], t["name"]) for t in s["tools"]} != {(a, b) for a, names in METHODS.items() for b in names} for s in schema):
        raise ValueError("NeMo meal original schema differs")
    measured = [e["response"]["result"] for e in journal if e["route"] == "/isolated/verify" and e["status"] == 200]
    if [(m["reward"], m["mask_sample"]) for m in measured] != [(0.0, False), (1.0, False), (0.0, False), (1.0, False), (None, True), (1.0, False), (None, True), (0.0, False), (0.0, False)]:
        # Journals are grouped by cookie client, preserving each client's order.
        raise ValueError("NeMo meal success/failure/unknown order differs")
    for m in measured:
        if m.get("evaluation_ref") and value(m["evaluation_ref"]) != m["evaluation"]:
            raise ValueError("NeMo meal evaluator response differs from retained original output")
    wrong = measured[2]
    if wrong["evaluation"]["upstream_passed"] is not True or wrong["evaluation"]["supplemental_passed"] is not False:
        raise ValueError("Original evaluator blind spot was not independently rejected")
    fault = value(receipt["fault_ref"])
    if fault["original_ref"] not in retained or fault["restored_sha256"] != retained[fault["original_ref"]]:
        raise ValueError("Infrastructure fault restoration bytes differ")
    if receipt["calendar_counts"] != [21, 0] or receipt["shared_before"] != receipt["shared_after"]:
        raise ValueError("NeMo meal isolation or shared data differs")
    return {"checks": len(CHECKS), "nemo_second_task_enabled": True, "original_tasks": 2, "episodes": 2,
        "training_admissions": 0, "recovery_and_expiry_acceptance": "pending"}


def audit_meal_lifecycle(root, path):
    from integrations.isolated_audit import audit_isolated, CHECKS as ISOLATED_CHECKS
    mapping = {"nemo-meal-recovery-diagnostic-v1": ISOLATED_CHECKS["nemo-isolated-recovery-diagnostic-v1"],
        "nemo-meal-expiry-diagnostic-v1": ISOLATED_CHECKS["nemo-isolated-expiry-diagnostic-v1"]}
    receipt = read_json(path, root)
    if (receipt.get("task_id") != "howtocook-meal-plan-gcal" or receipt.get("training_eligible") is not False
        or receipt.get("reference_solutions_read") is not False or receipt.get("verified_task_bodies_read") is not False):
        raise ValueError("NeMo meal lifecycle task or scope differs")
    result = audit_isolated(root, path, expected_task="howtocook-meal-plan-gcal", checks_by_schema=mapping)
    journal = read_json(path.parent / "http_journal.json", root)
    seeded = [e["request"] for e in journal if e["route"] == "/isolated/seed_session" and e["status"] == 200]
    expected_seeds = 2 if receipt["schema_version"] == "nemo-meal-recovery-diagnostic-v1" else 1
    if len(seeded) != expected_seeds: raise ValueError("NeMo meal lifecycle seed count differs")
    for seed in seeded:
        base = root / ".runtime/native-episodes" / seed["resources_session_id"]
        owner = read_json(base / "cluster_owner.json", root)
        profile = EpisodeProfile(root, seed["resources_session_id"], owner["slot"])
        profile.validate_record(owner)
        if read_json(base / "meal-task-session/task_profile.json", root) != TaskRouter(root, "isolated").authorize(seed):
            raise ValueError("NeMo meal lifecycle profile differs")
        require_dependents_closed(profile)
        read_json(base / "dependent_processes.json", root)
        closed = read_json(base / "meal-task-session/services-closed.json", root)
        if closed["state"] != "closed" or set(closed["sessions"]) != set(METHODS):
            raise ValueError("NeMo meal lifecycle did not close both original MCP servers")
        track_absence(profile.data / "postmaster.pid", root)
        if read_json(base / "resources-session.json", root)["sessions"][profile.resource_id]["state"] != "closed":
            raise ValueError("NeMo meal lifecycle left a worker identity active")
    if receipt["schema_version"] == "nemo-meal-expiry-diagnostic-v1" and receipt.get("normal_lease_restored_seconds") != 900:
        raise ValueError("NeMo meal expiry did not restore the normal lease")
    return {**result, "original_meal_episodes": len(seeded), "nemo_second_task_enabled": True}
