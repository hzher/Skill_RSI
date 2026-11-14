"""Dataset route qualification after independent seed review; no optimization."""
import argparse
import hashlib
import json
from pathlib import Path

from evaluation.firewall import EvaluationFirewall
from integrations.task_profiles import build_registry, read_owned, resolve_profile, PROFILE_PATH, IMPLEMENTATION_FILES
from training.skill_contract import TrainingRejected
from workenv.core import save

ROOT = Path(__file__).resolve().parents[1]


def profile_catalog_current(root=ROOT):
    try: return json.loads(read_owned(root, PROFILE_PATH)) == build_registry(root)
    except (OSError, ValueError, SyntaxError, KeyError, TypeError): return False


def validate_task_profile(provenance, firewall, *, resource_backend="shared", root=ROOT):
    # A profile cannot substitute for the independently admitted provenance.
    firewall.check_training_record(provenance)
    try:
        registry = json.loads(read_owned(root, PROFILE_PATH))
        matches = [p for p in registry["profiles"].values() if p["task_id"] == provenance["task_id"]]
        if len(matches) != 1: raise ValueError("No unique adapter profile")
        profile = matches[0]
        resolved = resolve_profile(registry, task_id=provenance["task_id"], source_commit=provenance["source_commit"],
                                   task_config_sha256=profile["task_config_sha256"], resource_backend=resource_backend, root=root)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError):
        raise TrainingRejected("Admitted seed has no current source-bound task profile for its resource backend") from None
    return {"profile_id": profile["profile_id"], "profile_sha256": resolved["profile_sha256"],
            "resource_backend": resource_backend, "scope": "reviewed_provenance_and_source_contract_only",
            "runtime_scheduler_activation": False, "training_admission": False}


def inspect_admission(root=ROOT, resource_backend="shared"):
    if resource_backend not in ("shared", "isolated"): raise ValueError("Unknown Generator resource backend")
    firewall = EvaluationFirewall.load(root / "evaluation/toolathlon_verified.manifest.json", root / "training/seed_registry.json")
    seeds = json.loads(read_owned(root, "training/seed_registry.json"))["admitted_seeds"]
    errors = 0
    for seed in seeds:
        try: validate_task_profile(seed, firewall, resource_backend=resource_backend, root=root)
        except (ValueError, TrainingRejected): errors += 1
    current = profile_catalog_current(root)
    return {"schema_version": "generator-task-profile-admission-v1", "passed": current and errors == 0,
            "source_profile_catalog_current": current, "checked_admitted_seeds": len(seeds), "profile_binding_errors": errors,
            "resource_backend": resource_backend, "ready_for_training": False, "new_training_admissions": 0,
            "real_model_calls": 0, "training_updates": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-backend", choices=("shared", "isolated"), default="shared")
    args = parser.parse_args()
    report = inspect_admission(resource_backend=args.resource_backend)
    paths = ("training/profile_admission.py", "training/generator_dataset.py", "training/preflight.py", "training/launch_verl.py",
             "training/skill_contract.py", "evaluation/firewall.py", "integrations/task_profiles.py", PROFILE_PATH,
             "training/seed_registry.json", "evaluation/toolathlon_verified.manifest.json", "configs/mainline_sources.lock.json",
             "training/toolathlon_seed_capabilities.json", *IMPLEMENTATION_FILES)
    report["source_sha256"] = {name: hashlib.sha256(read_owned(ROOT, name)).hexdigest() for name in paths}
    save(ROOT / "evidence/mainline/task_profile_admission.json", report)
    print(json.dumps({k: report[k] for k in ("passed", "source_profile_catalog_current", "checked_admitted_seeds", "profile_binding_errors", "ready_for_training")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
