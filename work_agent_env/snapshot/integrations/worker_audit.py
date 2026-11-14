"""Read-only evidence checks for private resource workers, separate from NeMo runs."""
import hashlib

from integrations.episode_cluster import EpisodeProfile
from integrations.isolation_audit import evidence_binding
from integrations.mainline_audit import read_json, source_binding, track_absence
from workenv.core import contained


def audit_workers(root, receipt_path):
    receipt = read_json(receipt_path, root)
    required = {"two_owned_worker_processes_and_clusters", "slot_0_original_tools", "slot_0_real_failure",
        "slot_1_original_tools", "slot_1_real_failure", "concurrent_original_mcp_requests_complete",
        "worker_mutation_does_not_cross_clusters", "cross_worker_resource_id_rejected", "workers_exit_and_owned_clusters_stop"}
    checks = receipt.get("checks", [])
    if (receipt.get("schema_version") != "isolated-resource-workers-probe-v1" or receipt.get("passed") is not True
        or len(checks) != len(required) or {c.get("name") for c in checks} != required or any(c.get("passed") is not True for c in checks)):
        raise ValueError("Private worker diagnostic did not complete")
    if (receipt.get("nemo_multi_episode_enabled") is not False or receipt.get("execution_kind") != "private_resource_rpc_diagnostic"
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates"))):
        raise ValueError("Private worker probe was promoted to NeMo/model/training work")
    source_binding(root, receipt.get("source_sha256"))
    evidence_binding(root, receipt.get("evidence_sha256"))
    profiles = receipt.get("profiles", [])
    if len(profiles) != 2 or {p.get("slot") for p in profiles} != {0, 1} or len({p.get("resource_id") for p in profiles}) != 2:
        raise ValueError("Private workers are not distinct resource profiles")
    observed = receipt.get("sql_diagnostic_observation", {})
    if observed != {"query": "SELECT count(*) FROM gsheet.spreadsheets", "slot_order": [0, 1], "counts": [1, 0]}:
        raise ValueError("Private worker database isolation observation changed")
    for record in profiles:
        profile = EpisodeProfile(root, record["resource_id"], record["slot"])
        profile.validate_record(record)
        if read_json(profile.base / "cluster_owner.json", root) != record or (profile.data / "postmaster.pid").exists():
            raise ValueError("Worker cluster ownership changed or postmaster was not stopped")
        track_absence(profile.data / "postmaster.pid", root)
    journal = receipt_path.parent / "resource_journal.json"
    events = read_json(journal, root, limit=8 * 1024 * 1024)
    if hashlib.sha256(journal.read_bytes()).hexdigest() != receipt.get("journal_sha256") or not 1 <= len(events) <= 50:
        raise ValueError("Private resource journal changed or exceeds budget")
    for record in profiles:
        sid = record["resource_id"]
        measured = [e for e in events if e.get("resource_id") == sid and e.get("route") == "/verify" and e.get("response", {}).get("status") == 200]
        if len(measured) != 1: raise ValueError("Private worker measured result missing")
        event = measured[0]
        value = event["response"]["result"]
        if event["request"].get("resources_session_id") != sid or value.get("reward") != 0 or value.get("mask_sample") is not False:
            raise ValueError("Private worker negative result is not bound or valid")
        reference = value.get("evaluation_ref", "")
        if not reference.startswith("runs/") or not reference.endswith("/evaluation.json"):
            raise ValueError("Private worker evaluator reference is invalid")
        original = read_json(contained(root, reference), root)
        run = read_json(contained(root, value["run_ref"]) / "run.json", root)
        if original != value.get("evaluation") or original.get("task_passed") is not False or run.get("resource_profile") != record:
            raise ValueError("Private worker retained evaluation/resource mismatch")
    return {"passed": True, "private_workers": 2, "nemo_multi_episode_enabled": False,
            "receipt": receipt_path.relative_to(root).as_posix()}
