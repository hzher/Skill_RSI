"""Audit cross-task isolation and reuse of the same NeMo resource pool."""
import hashlib

from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_absence, track_input
from integrations.runtime_identity import expected_identity
from integrations.task_routing import TaskRouter
from workenv.core import contained, no_links

CHECKS = ("two_task_types_share_the_resource_pool", "task_specific_original_schemas", "task_specific_database_schemas",
    "third_resource_rejected_without_allocating", "cross_task_tools_rejected", "task_specific_empty_failures",
    "both_original_and_supplemental_evaluators_pass", "closing_tax_preserves_meal", "tax_slot_reused_by_fresh_meal",
    "fresh_meal_is_empty_and_old_meal_still_passes", "all_closed_identities_remain_closed", "shared_state_and_other_ledgers_unchanged")
EXTRA_SOURCES = ("scripts/verify_nemo_mixed_tasks.py", "scripts/diagnostic_tax_solution.py", "scripts/diagnostic_meal_plan.py",
    "integrations/nemo_mixed_audit.py", "integrations/nemo_http.py", "scripts/verify_calendar_isolation.py")


def validate_measurements(measured, journal, seeds):
    if any(type(m["result"]["reward"]) not in (int, float) or m["result"].get("mask_sample") is not False for m in measured):
        raise ValueError("Mixed task rewards require known numeric measurements")
    if [(m["task_index"], m["result"]["reward"]) for m in measured] != [(0, 0), (1, 0), (0, 1), (1, 1), (1, 1), (2, 0), (1, 1)]:
        raise ValueError("Mixed task verifier dispatch or isolation differs")
    for i, seed in enumerate(seeds):
        actual = []
        for event in journal:
            if event["route"] != "/isolated/verify" or event["status"] != 200: continue
            request, response = event["request"], event["response"]
            if request["verification_input"]["resources_session_id"] != seed["resources_session_id"]: continue
            if (request["task_id"] != seed["task_id"] or request["episode_id"] != seed["episode_id"]
                or response["task_id"] != request["task_id"] or response["episode_id"] != request["episode_id"]):
                raise ValueError("Mixed task measurement was rebound to another task or episode")
            actual.append(response["result"])
        if actual != [m["result"] for m in measured if m["task_index"] == i]:
            raise ValueError("Mixed task measurement summary differs from raw NeMo HTTP responses")


def audit_mixed(root, path):
    receipt = read_json(path, root)
    if (receipt.get("schema_version") != "nemo-mixed-original-tasks-diagnostic-v1" or receipt.get("passed") is not True
        or receipt.get("framework") != "nemo_gym" or receipt.get("execution_kind") != "scripted_heterogeneous_resource_diagnostic"
        or [c["name"] for c in receipt["checks"]] != list(CHECKS) or any(c["passed"] is not True for c in receipt["checks"])
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates", "training_admissions"))
        or any(receipt.get(k) is not False for k in ("training_eligible", "reference_solutions_read", "verified_task_bodies_read"))):
        raise ValueError("Mixed original task diagnostic scope differs")
    expected = {**expected_identity(root), **{name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in EXTRA_SOURCES}}
    if receipt["source_sha256"] != expected: raise ValueError("Mixed original task source inventory differs")
    source_binding(root, receipt["source_sha256"])
    retained = receipt["retained_sha256"]
    if not 20 <= len(retained) <= 192: raise ValueError("Mixed task retained inventory exceeds budget")
    for name, digest in retained.items():
        member = contained(root, name)
        no_links(member, root)
        if member.stat().st_size > 4 * 1024 * 1024: raise ValueError("Mixed task evidence exceeds byte budget")
        actual = hashlib.sha256(member.read_bytes()).hexdigest()
        track_input(member, root, actual)
        if actual != digest: raise ValueError("Mixed task evidence changed")
    def value(name):
        if name not in retained: raise ValueError("Mixed task evidence lacks retained binding")
        return read_json(contained(root, name), root)
    seeds = receipt["seeds"]
    if len(seeds) != 3 or [s["task_id"]["task_id"] for s in seeds] != ["wc-sales-tax-summary", "howtocook-meal-plan-gcal", "howtocook-meal-plan-gcal"]:
        raise ValueError("Mixed task seed identities differ")
    profiles = []
    for i, seed in enumerate(seeds):
        raw = receipt["profiles"][str(i)]
        profile = EpisodeProfile(root, seed["resources_session_id"], raw["slot"])
        profile.validate_record(raw)
        if value((profile.base / "cluster_owner.json").relative_to(root).as_posix()) != raw: raise ValueError("Mixed task owner differs")
        if value((profile.base / "resources-session.json").relative_to(root).as_posix())["sessions"][profile.resource_id]["state"] != "closed":
            raise ValueError("Mixed task worker ledger remains active")
        require_dependents_closed(profile)
        if i:
            read_json(profile.base / "dependent_processes.json", root)
            if value((profile.base / "meal-task-session/task_profile.json").relative_to(root).as_posix()) != TaskRouter(root, "isolated").authorize(seed):
                raise ValueError("Mixed meal task source profile differs")
        track_absence(profile.data / "postmaster.pid", root)
        profiles.append(profile)
    if profiles[0].slot == profiles[1].slot or profiles[0].slot != profiles[2].slot or profiles[0].data == profiles[2].data:
        raise ValueError("Cross-task slot reuse did not retain separate data directories")
    if receipt["sql_schemas"] != [["gsheet", "wc"], ["gcal"]] or receipt["reused_slot_schemas"] != ["gcal"]:
        raise ValueError("Mixed task database schema scope differs")
    journal = value(receipt["journal_ref"])
    for seed in seeds:
        successful = [e for e in journal if e["route"] == "/isolated/seed_session" and e["status"] == 200 and e["request"] == seed]
        refused = [e for e in journal if e["route"] == "/isolated/seed_session" and e["status"] == 409 and e["request"] == seed]
        if len(successful) != 1 or not refused: raise ValueError("Mixed task seed/reseed lineage differs")
    measured = receipt["measurements"]
    validate_measurements(measured, journal, seeds)
    for m in measured:
        original = value(m["result"]["evaluation_ref"])
        if original != m["result"]["evaluation"] or m["result"]["mask_sample"] is not False:
            raise ValueError("Mixed task measurement lost its original evaluator binding")
        if m["result"]["reward"] == 1 and not all(original.get(k) is True for k in ("task_passed", "upstream_passed", "supplemental_passed")):
            raise ValueError("Mixed task success lacked both verifiers")
    if receipt["shared_before"] != receipt["shared_after"] or len(receipt["shared_before"]) != 6:
        raise ValueError("Mixed task shared database changed")
    return {"checks": len(CHECKS), "task_types": 2, "episodes": 3, "cross_task_slot_reuse": True, "training_admissions": 0}
