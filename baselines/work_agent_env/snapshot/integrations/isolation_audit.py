"""Audit isolated resource probes without treating them as model/NeMo rollouts."""
import hashlib
import re

from integrations.episode_cluster import EpisodeProfile
from integrations.mainline_audit import read_json, source_binding, track_input, track_absence
from workenv.core import contained


def evidence_binding(root, hashes):
    if not isinstance(hashes, dict) or not 1 <= len(hashes) <= 128:
        raise ValueError("Missing/bounded isolation evidence inventory")
    total = 0
    for name, expected in hashes.items():
        if not isinstance(name, str) or not name.startswith((".runtime/native-episodes/", "runs/")):
            raise ValueError("Isolation evidence escaped its owned directories")
        path = contained(root, name)
        total += path.stat().st_size
        if path.stat().st_size > 16 * 1024 * 1024 or total > 32 * 1024 * 1024 or not re.fullmatch("[0-9a-f]{64}", expected):
            raise ValueError("Invalid isolation evidence hash/budget")
        with path.open("rb") as stream: actual = hashlib.file_digest(stream, "sha256").hexdigest()
        track_input(path, root, actual)
        if actual != expected: raise ValueError("Isolation evidence changed: " + name)
    return True


def audit_isolation(root, receipt_path):
    receipt = read_json(receipt_path, root)
    if receipt.get("schema_version") != "native-episode-cluster-probe-v1" or receipt.get("passed") is not True:
        raise ValueError("Isolation probe did not pass")
    if receipt.get("execution_kind") != "resource_isolation_diagnostic" or receipt.get("nemo_concurrency_enabled") is not False:
        raise ValueError("Resource probe was promoted to a NeMo concurrency result")
    if any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates")):
        raise ValueError("Resource probe cannot claim model calls or training")
    required = {"owned_schema_dump_completed", "separate_owned_data_directories_and_hosts", "same_table_and_key_have_independent_values",
                "mutation_does_not_cross_clusters", "original_shared_resource_unchanged", "both_owned_postmasters_stopped",
                "async_resource_context_restored", "reset_first_episode_preserves_second_completed_task", "cross_episode_run_rejected"}
    required |= {f"slot_{slot}_{suffix}" for slot in (0, 1) for suffix in (
        "original_preprocess", "original_evaluation", "seed_read_only_orders_present", "native_adapter_measured_failure", "original_mcp_and_full_evaluators_pass")}
    checks = receipt.get("checks", [])
    if len(checks) != len(required) or {c.get("name") for c in checks} != required or any(c.get("passed") is not True for c in checks):
        raise ValueError("Isolation probe coverage is incomplete")
    source_binding(root, receipt.get("source_sha256"))
    hashes = receipt.get("evidence_sha256")
    evidence_binding(root, hashes)
    def retained(name):
        if name not in hashes: raise ValueError("Required isolation evidence was not bound: " + name)
        return contained(root, name)
    profiles = receipt.get("profiles", [])
    if len(profiles) != 2 or {p.get("slot") for p in profiles} != {0, 1} or len({p.get("resource_id") for p in profiles}) != 2:
        raise ValueError("Expected two distinct owned resource profiles")
    by_id = {}
    for record in profiles:
        profile = EpisodeProfile(root, record["resource_id"], record["slot"])
        profile.validate_record(record)
        relative = profile.base.relative_to(root).as_posix()
        profile.validate_record(read_json(retained(relative + "/cluster_owner.json"), root))
        retained(relative + "/pgdata/PG_VERSION")
        track_absence(profile.data / "postmaster.pid", root)
        if b"Overall: FAIL" not in retained(relative + "/evaluation.stdout.txt").read_bytes():
            raise ValueError("Original isolated negative evaluator was not observed")
        by_id[profile.resource_id] = profile
    results = receipt.get("business_results", [])
    if len(results) != 2 or {r["resource_id"] for r in results} != set(by_id):
        raise ValueError("Original MCP checks did not cover both isolated resources")
    for row in results:
        profile = by_id[row["resource_id"]]
        for phase in ("negative", "positive"):
            path = retained(row[phase + "_evaluation_ref"])
            metadata = read_json(retained((path.parent / "run.json").relative_to(root).as_posix()), root)
            if metadata.get("resource_profile") != profile.record() or metadata.get("task_id") != "wc-sales-tax-summary":
                raise ValueError("Retained task evaluation belongs to a different resource")
            value = read_json(path, root)
            if value.get("evaluation_status") != "completed" or value.get("task_passed") is not (phase == "positive"):
                raise ValueError("Isolated task measurement disagrees with its phase")
            if phase == "positive" and (value.get("upstream_passed") is not True or value.get("supplemental_passed") is not True):
                raise ValueError("Full isolated task verification did not pass")
    task = root / ".upstream/toolathlon-gym/tasks/finalpool/wc-sales-tax-summary"
    if receipt.get("upstream_sha256") != {stage: hashlib.sha256((task / stage / "main.py").read_bytes()).hexdigest() for stage in ("preprocess", "evaluation")}:
        raise ValueError("Pinned original task scripts changed after the isolation probe")
    return {"passed": True, "isolated_resources": 2, "original_mcp_and_verifier_paths": 2,
            "nemo_concurrency_enabled": False, "receipt": receipt_path.relative_to(root).as_posix()}


def audit_episode_recovery(root, receipt_path):
    receipt = read_json(receipt_path, root)
    required = {"exact_owned_worker_identity", "worker_resource_profile_matches", "live_lease_owner_refused_without_mutation",
                "actual_owned_cluster_live_before_fault", "worker_stopped_and_lease_retained", "owned_postmaster_survives_worker_fault",
                "orphan_cluster_stopped_and_slot_closed", "repeated_recovery_is_idempotent"}
    checks = receipt.get("checks", [])
    if (receipt.get("schema_version") != "native-episode-recovery-probe-v1" or receipt.get("passed") is not True
        or len(checks) != len(required) or {c.get("name") for c in checks} != required or any(c.get("passed") is not True for c in checks)):
        raise ValueError("Owned worker recovery probe is incomplete")
    if receipt.get("execution_kind") != "resource_fault_diagnostic" or receipt.get("nemo_concurrency_enabled") is not False:
        raise ValueError("Resource recovery was promoted to NeMo concurrency")
    if any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates")):
        raise ValueError("Resource fault diagnostic cannot claim model or training work")
    source_binding(root, receipt.get("source_sha256"))
    hashes = receipt.get("evidence_sha256")
    evidence_binding(root, hashes)
    record = receipt["profile"]
    profile = EpisodeProfile(root, record["resource_id"], record["slot"])
    profile.validate_record(record)
    owner_ref = (profile.base / "cluster_owner.json").relative_to(root).as_posix()
    if owner_ref not in hashes or read_json(profile.base / "cluster_owner.json", root) != record:
        raise ValueError("Recovered cluster ownership record changed")
    recovery = receipt.get("recovery") or {}
    reference = recovery.get("lease_archive_ref", "")
    if reference not in hashes or not reference.startswith(profile.base.relative_to(root).as_posix() + "/recovered-slot-lease-"):
        raise ValueError("Recovered lease was not retained for this resource")
    lease = read_json(contained(root, reference), root)
    if lease.get("process") != receipt.get("fault_worker_identity") or lease.get("kind") != "native_episode_slot" or lease.get("workspace_owner") != str(root):
        raise ValueError("Faulted worker differs from the original lease owner")
    if recovery.get("state") != "stopped" or recovery.get("data_retained") is not True or (profile.data / "postmaster.pid").exists():
        raise ValueError("Recovered postmaster was not confirmed stopped")
    track_absence(profile.data / "postmaster.pid", root)
    return {"passed": True, "owned_worker_fault_verified": True, "data_retained": True,
            "nemo_concurrency_enabled": False, "receipt": receipt_path.relative_to(root).as_posix()}
