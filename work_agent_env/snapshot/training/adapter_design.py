"""Plan original-task adapter work using pinned public configuration metadata."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from integrations.task_profiles import build_registry, read_owned, PROFILE_PATH
from workenv.core import save

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ("configs/mainline_sources.lock.json", "training/toolathlon_seed_capabilities.json", PROFILE_PATH)


def design_adapter(root=ROOT, task_id="howtocook-meal-plan-gcal"):
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", task_id):
        raise ValueError("Invalid public seed task identity")
    lock, capabilities, registry = [json.loads(read_owned(root, name)) for name in INPUTS]
    pin = lock["sources"]["toolathlon_gym"]["commit"]
    if capabilities.get("source") != "toolathlon_gym" or capabilities.get("commit") != pin or registry != build_registry(root):
        raise ValueError("Adapter design requires current pinned configuration and source-bound profiles")
    row = capabilities.get("tasks", {}).get(task_id)
    if not row or row.get("configuration_status") != "inspected":
        raise ValueError("Requested task configuration has not been inspected")
    if (not re.fullmatch(r"[0-9a-f]{40}", row.get("config_git_blob", ""))
        or not re.fullmatch(r"[0-9a-f]{64}", row.get("config_sha256", ""))):
        raise ValueError("Requested task lacks a canonical Git configuration identity")
    services, tools = row["needed_mcp_servers"], row["needed_local_tools"]
    if any(not isinstance(names, list) or len(names) > 128 or len(set(names)) != len(names)
           or any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", n) for n in names)
           for names in (services, tools)):
        raise ValueError("Invalid service/local-tool metadata")
    profiles = list(registry["profiles"].values())
    represented = {name for profile in profiles for name in profile["mcp_methods"]}
    matches = [profile["profile_id"] for profile in profiles if profile["task_id"] == task_id]
    return {"schema_version": "original-task-adapter-design-v1", "source": "toolathlon_gym", "source_commit": pin,
            "task_id": task_id, "task_config_git_blob": row["config_git_blob"], "task_config_sha256": row["config_sha256"],
            "existing_task_profile_ids": matches, "declared_mcp_services": sorted(services),
            "service_names_represented_in_existing_profiles": sorted(set(services) & represented),
            "declared_services_without_existing_mcp_profile": sorted(set(services) - represented),
            "method_compatibility": "unknown_requires_original_mcp_schema_and_task_review",
            "declared_local_tools_requiring_scope_review": sorted(tools),
            "preprocess_script_present": row["preprocess_script_present"],
            "evaluation_script_present": row["evaluation_script_present"],
            "file_asset_count": row["file_asset_count"],
            "acceptance_gates": ["review_pinned_original_task_and_mcp_schemas", "original_preprocess_strict_failure_handling",
                "task_scoped_mcp_allowlist_and_artifact_contract", "original_and_supplemental_evaluator_cases",
                "per_episode_state_reset_and_database_isolation", "timezone_and_external_service_determinism",
                "actual_nemo_success_failure_and_infrastructure_unknown", "close_restart_expiry_and_no_replay",
                "source_family_alias_review_before_training"],
            "scope": "Design from public configuration metadata only; shared service names do not prove method coverage",
            "runtime_activation": False, "training_admission": False, "reference_solutions_read": False,
            "verified_task_bodies_read": False, "real_model_calls": 0, "training_updates": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default="howtocook-meal-plan-gcal")
    args = parser.parse_args()
    report = design_adapter(task_id=args.task)
    from integrations.task_profiles import IMPLEMENTATION_FILES
    names = (*INPUTS, *IMPLEMENTATION_FILES, "integrations/task_profiles.py", "training/adapter_design.py")
    report["source_sha256"] = {name: hashlib.sha256(read_owned(ROOT, name)).hexdigest() for name in names}
    save(ROOT / "evidence/mainline/next_seed_adapter_design.json", report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__": raise SystemExit(main())
