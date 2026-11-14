"""Audit real NeMo isolated episodes and their infrastructure-unknown outcomes."""
import hashlib

from integrations.episode_cluster import EpisodeProfile
from integrations.mainline_audit import read_json, source_binding, initial_state_binding, track_absence
from workenv.core import contained

CHECKS = {
    "nemo-isolated-original-diagnostic-v1": {"two_sessions_active", "idempotent_seed", "third_slot_busy", "cookie_and_source_isolation",
        "cross_episode_rejected", "mismatched_task_rejected", "two_measured_business_failures", "concurrent_nemo_original_tools_solved_tasks",
        "two_full_evaluators_pass", "closing_first_preserves_second", "late_seed_cannot_recreate_closed", "unknown_close_tombstone_enforced",
        "idempotent_close_and_episode_binding", "closed_sessions_mask_unknown", "both_workers_stopped_and_other_ledgers_unchanged"},
    "nemo-isolated-recovery-diagnostic-v1": {"owned_isolated_session_before_fault", "exact_owned_bridge_identity", "old_bridge_exited_global_lock_retained",
        "isolated_orphan_closed_after_restart", "owned_worker_and_postmaster_stopped", "late_seed_refused", "orphan_unknown_masked",
        "fresh_isolated_session_after_recovery", "same_nemo_process"},
    "nemo-isolated-expiry-diagnostic-v1": {"isolated_short_lease_acquired", "expired_isolated_session_unknown_masked",
        "expired_worker_closed_and_postmaster_stopped", "expired_identity_cannot_reseed", "expired_close_idempotent"},
}


def audit_isolated(root, receipt_path, *, expected_task="wc-sales-tax-summary", checks_by_schema=None):
    receipt = read_json(receipt_path, root)
    expected = (CHECKS if checks_by_schema is None else checks_by_schema).get(receipt.get("schema_version"))
    checks = receipt.get("checks", [])
    if not expected or receipt.get("passed") is not True or len(checks) != len(expected) or {c.get("name") for c in checks} != expected or any(c.get("passed") is not True for c in checks):
        raise ValueError("NeMo isolated diagnostic did not complete all required checks")
    if receipt.get("training_concurrency_enabled") is not False or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates")):
        raise ValueError("Isolated diagnostic cannot claim model/training execution")
    source_binding(root, receipt.get("source_sha256"))
    journal = receipt_path.parent / "http_journal.json"
    events = read_json(journal, root, limit=16 * 1024 * 1024)
    if hashlib.sha256(journal.read_bytes()).hexdigest() != receipt.get("journal_sha256") or not isinstance(events, list) or not 1 <= len(events) <= 250:
        raise ValueError("NeMo isolated HTTP journal changed or exceeds its budget")
    valid, unknown, positive, negative = 0, 0, 0, 0
    resources = set()
    for event in events:
        if event.get("route") != "/isolated/verify" or event.get("status") != 200: continue
        request, reply = event["request"], event["response"]
        if reply.get("episode_id") != request.get("episode_id") or reply.get("task_id") != request.get("task_id"):
            raise ValueError("Isolated verification reply task/episode changed")
        if request["task_id"].get("task_id") != expected_task or not request["task_id"].get("taskset", "").startswith("toolathlon_gym:"):
            raise ValueError("Isolated diagnostic was relabeled as another source")
        sid = request["verification_input"]["resources_session_id"]
        value = reply["result"]
        if value.get("mask_sample") is True:
            if value.get("reward") is not None: raise ValueError("Unknown isolated outcome was converted to a numeric reward")
            unknown += 1
        elif value.get("mask_sample") is False:
            reward = value.get("reward")
            if type(reward) not in (float, int) or reward not in (0, 1): raise ValueError("Invalid isolated task reward")
            reference = value.get("evaluation_ref", "")
            if not reference.startswith("runs/") or not reference.endswith("/evaluation.json"):
                raise ValueError("Missing retained isolated original evaluation")
            target = contained(root, reference)
            original = read_json(target, root)
            if original != value.get("evaluation") or original.get("evaluation_status") != "completed" or type(original.get("task_passed")) is not bool or float(original["task_passed"]) != reward:
                raise ValueError("Isolated reward differs from original evaluator evidence")
            run = initial_state_binding(root, target.parent.parent)
            seeded = read_json(target.parent.parent / "nemo_binding.json", root)
            if seeded.get("episode_id") != request["episode_id"] or seeded.get("task_id") != request["task_id"]:
                raise ValueError("Isolated evaluation differs from the originally seeded episode")
            profile_record = run.get("resource_profile") or {}
            profile = EpisodeProfile(root, sid, profile_record.get("slot"))
            profile.validate_record(profile_record)
            if (profile.data / "postmaster.pid").exists(): raise ValueError("Diagnostic isolated cluster was not stopped")
            track_absence(profile.data / "postmaster.pid", root)
            if reward == 1 and (original.get("upstream_passed") is not True or original.get("supplemental_passed") is not True):
                raise ValueError("Isolated successful reward lacked full original/supplemental checks")
            resources.add(sid)
            valid += 1
            positive += reward == 1
            negative += reward == 0
        else:
            raise ValueError("Isolated measurement validity is unspecified")
    if receipt["schema_version"] == "nemo-isolated-original-diagnostic-v1":
        if (valid, unknown, positive, negative, len(resources)) != (5, 2, 3, 2, 2):
            raise ValueError("Isolated diagnostic did not measure both independent tasks and closure outcomes")
    elif (valid, unknown) != (0, 1):
        raise ValueError("Infrastructure diagnostic did not preserve the unknown measurement")
    return {"passed": True, "valid_measurements": valid, "unknown_measurements": unknown,
            "training_concurrency_enabled": False, "receipt": receipt_path.relative_to(root).as_posix()}
