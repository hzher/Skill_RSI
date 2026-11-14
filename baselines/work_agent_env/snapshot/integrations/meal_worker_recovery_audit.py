"""Audit a hard worker fault without pretending its interrupted local ledger closed."""
import hashlib

from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_absence, track_input
from integrations.runtime_identity import expected_identity
from integrations.task_routing import TaskRouter
from workenv.core import contained, no_links

CHECKS = ("original_calendar_write_and_artifact", "owned_worker_and_original_mcp_identities", "controlled_worker_exit",
    "nemo_masks_worker_loss", "old_mutation_not_replayed", "unresolved_dependents_keep_pg_and_slot", "original_children_closed",
    "explicit_reconciliation_preserves_pg_and_slot", "business_state_and_interrupted_ledger_unchanged",
    "nemo_close_recovers_pg_and_tombstones", "fresh_resource_after_recovery", "shared_database_unchanged")
EXTRA_SOURCES = ("scripts/verify_nemo_meal_worker_fault.py", "integrations/meal_worker_recovery_audit.py",
    "scripts/verify_meal_plan_recovery.py", "integrations/dependent_reconciliation.py", "integrations/nemo_http.py",
    "scripts/verify_calendar_isolation.py", "workenv/process_identity.py", "workenv/attested_process.py",
    "workenv/leases.py", "workenv/core.py")


def audit_worker_fault(root, path):
    receipt = read_json(path, root)
    if (receipt.get("schema_version") != "nemo-meal-hard-worker-fault-diagnostic-v1" or receipt.get("passed") is not True
        or receipt.get("cleanup_errors") != [] or receipt.get("execution_kind") != "owned_private_worker_fault_and_explicit_reconciliation"
        or [c["name"] for c in receipt["checks"]] != list(CHECKS) or any(c["passed"] is not True for c in receipt["checks"])
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates", "training_admissions"))
        or any(receipt.get(k) is not False for k in ("training_eligible", "reference_solutions_read", "verified_task_bodies_read"))):
        raise ValueError("Hard worker fault scope differs")
    expected = {**expected_identity(root), **{name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in EXTRA_SOURCES}}
    if receipt["source_sha256"] != expected: raise ValueError("Hard worker fault source inventory differs")
    source_binding(root, receipt["source_sha256"])
    retained = receipt["retained_sha256"]
    if not 12 <= len(retained) <= 128: raise ValueError("Worker fault retained inventory differs")
    for name, digest in retained.items():
        member = contained(root, name)
        no_links(member, root)
        if member.stat().st_size > 4 * 1024 * 1024: raise ValueError("Worker fault evidence exceeds budget")
        actual = hashlib.sha256(member.read_bytes()).hexdigest()
        track_input(member, root, actual)
        if actual != digest: raise ValueError("Worker fault evidence changed")
    def value(name):
        if name not in retained: raise ValueError("Worker fault input lacks retained binding")
        return read_json(contained(root, name), root)
    proof = value(receipt["observations_ref"])
    raw = receipt["profile"]
    profile = EpisodeProfile(root, raw["resource_id"], raw["slot"])
    profile.validate_record(raw)
    if value((profile.base / "cluster_owner.json").relative_to(root).as_posix()) != raw: raise ValueError("Worker fault owner differs")
    route = value((profile.base / "meal-task-session/task_profile.json").relative_to(root).as_posix())
    if route != TaskRouter(root, "isolated").authorize(receipt["seed"]): raise ValueError("Worker fault task profile differs")
    before, after = proof["worker_before"], proof["worker_after"]
    if proof.get("worker_fault_injected") is not True or before["status"] != "alive" or not before.get("creation_id") or after["pid"] != before["pid"] or after["status"] not in ("not_found", "exited"):
        raise ValueError("Worker fault exit identity differs")
    original = value(proof["originals"]["dependents"])
    require_dependents_closed(profile)
    current = value((profile.base / "dependent_processes.json").relative_to(root).as_posix())
    if original["state"] != "open" or original["profile"] != raw or set(original["children"]) != {"howtocook", "google_calendar"}:
        raise ValueError("Worker fault original child inventory differs")
    for name, child in original["children"].items():
        final = proof["children_after"][name]
        if child["profile"] != raw or final["pid"] != child["before"]["pid"] or final["status"] not in ("not_found", "exited"):
            raise ValueError("Worker fault original child closure differs")
        if current["children"][name]["before"] != child["before"]: raise ValueError("Worker reconciliation rebound an original child")
    result = proof["reconciliation"]
    if result["slot_released"] is not False or result["processes_terminated"] != 0 or result["requests_replayed"] != 0:
        raise ValueError("Worker reconciliation changed a slot, process or request")
    original_raw = contained(root, proof["originals"]["dependents"]).read_bytes()
    if current["reconciliation"]["original_sha256"] != hashlib.sha256(original_raw).hexdigest(): raise ValueError("Worker reconciliation original digest differs")
    archived = [contained(root, name).read_bytes() for name in result["archives"] if name in retained]
    if original_raw not in archived: raise ValueError("Worker reconciliation omitted the original bytes")
    local = profile.base / "resources-session.json"
    if local.read_bytes() != contained(root, proof["originals"]["worker_ledger"]).read_bytes() or value(local.relative_to(root).as_posix())["sessions"][profile.resource_id]["state"] != "active":
        raise ValueError("Interrupted local worker ledger was rewritten as a clean exit")
    if proof["gateway_closed_row"]["state"] != "closed" or proof["gateway_closed_row"]["binding"] != receipt["seed"]:
        raise ValueError("NeMo gateway identity was not durably closed")
    track_absence(profile.data / "postmaster.pid", root)
    if proof["events_before"] != proof["events_after_reconciliation"] or len(proof["events_before"]) != 1:
        raise ValueError("Worker fault changed or replayed an original mutation")
    if retained.get(proof["workbook_ref"]) != proof["workbook_sha256"]: raise ValueError("Worker fault workbook changed")
    journal = value(receipt["journal_ref"])
    tools = [e for e in journal if e["route"] == "/isolated/tool"]
    if len(tools) != 2 or [e["status"] for e in tools] != [200, 503] or tools[0]["request"] != tools[1]["request"]:
        raise ValueError("Worker loss replay rejection differs")
    measured = [e for e in journal if e["route"] == "/isolated/verify"]
    if [e["response"]["result"]["reward"] for e in measured] != [None, None, 0.0] or [e["response"]["result"]["mask_sample"] for e in measured] != [True, True, False]:
        raise ValueError("Worker loss or closed identity lost its unknown measurement")
    if (len(measured) != 3 or any(e["response"]["result"]["reward"] is not None or e["response"]["result"]["mask_sample"] is not True for e in measured[:2])
        or type(measured[2]["response"]["result"]["reward"]) not in (int, float) or measured[2]["response"]["result"]["mask_sample"] is not False):
        raise ValueError("Worker fault measurement type differs")
    if receipt["shared_before"] != receipt["shared_after"]: raise ValueError("Worker fault changed the shared database")
    return {"checks": len(CHECKS), "worker_hard_fault_tested": True, "explicit_dependent_reconciliation": True,
        "interrupted_worker_ledger_preserved": True, "gateway_identity_closed": True, "training_admissions": 0}
