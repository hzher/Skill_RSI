"""Project public task declarations and audited execution without admitting seeds.

The legacy adapter flags in the pinned capability inventory are historical
annotations, not evidence of current execution. This projection never contacts
a service, imports a task implementation, or changes the declaration inventory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

from integrations.mainline_audit import inputs_current, source_binding
from integrations.task_profiles import PROFILE_PATH, build_registry, profile_digest
from workenv.core import contained, save

ROOT = Path(__file__).resolve().parents[1]
DECLARATIONS = "training/toolathlon_seed_capabilities.json"
AUDIT = "evidence/mainline/consistency_audit.json"
REPORT = "evidence/mainline/task_runtime_capabilities.json"
SOURCE_PIN = "configs/mainline_sources.lock.json"
CONTRACTS = "evidence/mainline/contracts.json"
PROJECTION_SOURCE = "training/runtime_capabilities.py"
MAX_BYTES = 4 * 1024 * 1024
COMMON_CHECKS = ("contract_scope_and_bytes", "seed_service_capability_metadata",
                 "source_bound_task_profile_contracts", "input_inventory_stable")
# Explicit task/backend mappings prevent a service or one successful task from
# being interpreted as proof that every task using that service is supported.
EXECUTION_CHECKS = {
    ("wc-sales-tax-summary", "shared"): ("nemo-diagnostic", "actual_source_bound_task_routing"),
    ("wc-sales-tax-summary", "isolated"): ("nemo-isolated-original-diagnostic-v1", "actual_source_bound_task_routing"),
    ("howtocook-meal-plan-gcal", "isolated"): ("actual_nemo_original_meal_plan",),
    ("wc-order-analysis-gcal", "isolated"): ("actual_nemo_original_order_analysis",),
    ("wc-order-fulfillment-tracker", "isolated"): ("actual_nemo_fulfillment_project_diagnostic",),
    ("wc-shipping-zone-gsheet", "isolated"): ("actual_nemo_shipping_project_diagnostic",),
    ("wc-customer-order-gsheet-email", "isolated"): ("actual_nemo_vip_cloud_mail_diagnostic",),
}
LIFECYCLE_CHECKS = {
    ("wc-sales-tax-summary", "shared"): ("nemo-recovery", "nemo-expiry"),
    ("wc-sales-tax-summary", "isolated"): ("nemo-isolated-recovery-diagnostic-v1", "nemo-isolated-expiry-diagnostic-v1"),
    ("howtocook-meal-plan-gcal", "isolated"): ("actual_nemo_meal_lifecycle",),
    ("wc-order-analysis-gcal", "isolated"): ("actual_nemo_order_analysis_lifecycle",),
    ("wc-order-fulfillment-tracker", "isolated"): ("actual_nemo_fulfillment_lifecycle",),
    ("wc-shipping-zone-gsheet", "isolated"): ("actual_nemo_shipping_lifecycle",),
    ("wc-customer-order-gsheet-email", "isolated"): ("actual_nemo_vip_basic_lifecycle", "actual_nemo_vip_worker_fault"),
}


def _read(root, name):
    path = contained(root, name)
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Capability projection input exceeds its byte budget: " + name)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Capability projection input must be an object: " + name)
    return value, hashlib.sha256(raw).hexdigest()


def _declarations(root):
    lock, lock_hash = _read(root, SOURCE_PIN)
    metadata, metadata_hash = _read(root, DECLARATIONS)
    commit = lock.get("sources", {}).get("toolathlon_gym", {}).get("commit")
    tasks = metadata.get("tasks")
    if (not isinstance(commit, str) or not re.fullmatch("[0-9a-f]{40}", commit)
        or metadata.get("schema_version") != "toolathlon-seed-capabilities-v1"
        or metadata.get("source") != "toolathlon_gym" or metadata.get("commit") != commit
        or not isinstance(tasks, dict) or not 1 <= len(tasks) <= 1024):
        raise ValueError("Public declaration inventory does not match its source pin")
    for name, row in tasks.items():
        if (not isinstance(name, str) or not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_-]{0,127}", name)
            or not isinstance(row, dict) or row.get("configuration_status") not in
            ("inspected", "unknown_missing_local_git_object", "unknown_unsupported_configuration_metadata")):
            raise ValueError("Invalid public task declaration")
        if row["configuration_status"] == "inspected":
            for key in ("needed_mcp_servers", "needed_local_tools"):
                values = row.get(key)
                if (not isinstance(values, list) or len(values) > 128
                    or any(not isinstance(v, str) or not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_-]{0,63}", v) for v in values)):
                    raise ValueError("Invalid public service/tool declaration")
    return commit, tasks, {SOURCE_PIN: lock_hash, DECLARATIONS: metadata_hash}


def _profiles(root):
    try:
        registry, checksum = _read(root, PROFILE_PATH)
        if registry != build_registry(root):
            return {}, {"status": "stale", "reference": PROFILE_PATH, "sha256": checksum}
        profiles = registry["profiles"]
        by_task = {p["task_id"]: p for p in profiles.values()}
        if len(by_task) != len(profiles):
            raise ValueError("Ambiguous task profile")
        return by_task, {"status": "current", "reference": PROFILE_PATH, "sha256": checksum}
    except (OSError, ValueError, KeyError, TypeError, SyntaxError):
        return {}, {"status": "unavailable_or_invalid", "reference": PROFILE_PATH}


def _audit(root, required_hashes):
    """Require a complete, fresh audit; never fall back to an older success."""
    state = {"reference": AUDIT, "status": "missing_or_invalid", "current": False}
    try:
        receipt, checksum = _read(root, AUDIT)
        state["sha256"] = checksum
        checks = receipt.get("checks")
        if (receipt.get("schema_version") != "mainline-consistency-audit-v1"
            or receipt.get("passed") is not True or not isinstance(checks, list)
            or not 1 <= len(checks) <= 128 or any(not isinstance(c, dict)
                or not isinstance(c.get("name"), str) or c.get("passed") is not True for c in checks)):
            state["status"] = "failed_or_invalid"
            return {}, state
        by_name = {c["name"]: c for c in checks}
        if len(by_name) != len(checks) or not set(COMMON_CHECKS) <= set(by_name):
            state["status"] = "incomplete_or_ambiguous"
            return {}, state
        contracts, contracts_hash = _read(root, CONTRACTS)
        if contracts.get("passed") is not True or receipt.get("contracts_sha256") != contracts_hash:
            state["status"] = "contracts_unverified"
            return {}, state
        state["status"] = "stale"
        source_binding(root, receipt.get("source_sha256"))
        source_binding(root, contracts.get("source_sha256"))
        hashes = receipt.get("input_sha256", {})
        if (not isinstance(hashes, dict) or not isinstance(receipt.get("input_inventory"), dict)
            or any(hashes.get(name) != checksum for name, checksum in required_hashes.items())
            or not inputs_current(root, receipt)):
            state["status"] = "stale"
            return {}, state
        state.update(status="current_passed", current=True)
        return by_name, state
    except (OSError, ValueError, KeyError, TypeError):
        return {}, state


def project_capabilities(root=ROOT):
    """Return current evidence state; unknown is never a failed business reward."""
    root = Path(root).resolve()
    commit, declarations, hashes = _declarations(root)
    profiles, profile_state = _profiles(root)
    hashes[PROJECTION_SOURCE] = hashlib.sha256(contained(root, PROJECTION_SOURCE).read_bytes()).hexdigest()
    if "sha256" in profile_state:
        hashes[PROFILE_PATH] = profile_state["sha256"]
    checks, audit = _audit(root, hashes)
    tasks = {}
    for task, declaration in sorted(declarations.items()):
        profile = profiles.get(task)
        public = {"configuration_status": declaration["configuration_status"]}
        if declaration["configuration_status"] == "inspected":
            public.update({key: sorted(set(declaration[key])) for key in ("needed_mcp_servers", "needed_local_tools")})
        backends = {}
        if profile is not None:
            for backend in profile["resource_backends"]:
                key = (task, backend)
                execution = EXECUTION_CHECKS.get(key, ())
                lifecycle = LIFECYCLE_CHECKS.get(key, ())
                executed = bool(execution and all(name in checks for name in execution))
                recovered = bool(executed and lifecycle and all(name in checks for name in lifecycle))
                backends[backend] = {
                    "measurement_mode": profile.get("measurement_mode", "original_and_supplemental"),
                    "official_benchmark_score_available": False,
                    "execution": "verified_by_current_audit" if executed else "unverified",
                    "recovery_and_expiry": "verified_by_current_audit" if recovered else "unverified",
                    "required_execution_checks": list(execution),
                    "required_lifecycle_checks": list(lifecycle),
                }
        tasks[task] = {
            "upstream_declaration": public,
            "adapter_profile": {"status": "registered_current" if profile else
                "not_registered" if profile_state["status"] == "current" else "unverified_registry",
                "profile_id": profile["profile_id"] if profile else None,
                "profile_sha256": profile_digest(profile) if profile else None},
            "observed_runtime_backends": backends,
        }
    executed_tasks = [task for task, row in tasks.items() if any(
        b["execution"] == "verified_by_current_audit" for b in row["observed_runtime_backends"].values())]
    lifecycle_tasks = [task for task, row in tasks.items() if any(
        b["recovery_and_expiry"] == "verified_by_current_audit" for b in row["observed_runtime_backends"].values())]
    return {
        "schema_version": "toolathlon-runtime-capabilities-v1", "source": "toolathlon_gym", "commit": commit,
        "declaration_inventory": {"reference": DECLARATIONS, "sha256": hashes[DECLARATIONS],
            "legacy_adapter_annotations_used_as_runtime_evidence": False},
        "profile_registry": profile_state, "audit": audit,
        "summary": {"upstream_task_directories": len(tasks),
            "configurations_inspected": sum(d["configuration_status"] == "inspected" for d in declarations.values()),
            "current_registered_task_profiles": len(profiles),
            "tasks_with_current_execution_evidence": executed_tasks,
            "tasks_with_current_lifecycle_evidence": lifecycle_tasks,
            "current_execution_backend_contracts": sum(b["execution"] == "verified_by_current_audit"
                for row in tasks.values() for b in row["observed_runtime_backends"].values()),
            "tasks_without_current_execution_evidence": len(tasks) - len(executed_tasks)},
        "tasks": tasks, "input_sha256": hashes,
        "scope": "Public declarations, current source-bound profiles and prior audited execution; not a live readiness probe",
        "live_service_health_checked": False, "training_eligibility_evaluated": False,
        "training_admission_registry": "training/seed_registry.json", "automatic_training_admissions": 0,
        "real_model_calls": 0, "training_updates": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-current-audit", action="store_true",
        help="Return nonzero when execution evidence is missing, failed or stale")
    args = parser.parse_args()
    report = project_capabilities()
    save(contained(ROOT, REPORT), report)
    print(json.dumps({"report": REPORT, "audit": report["audit"]["status"], **report["summary"]}))
    return 1 if args.require_current_audit and not report["audit"]["current"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
