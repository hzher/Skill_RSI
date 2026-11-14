"""Read-only training readiness inspection; never install GPU packages or run training."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

from evaluation.firewall import EvaluationFirewall

ROOT = Path(__file__).resolve().parents[1]


def inspect(resource_backend="shared"):
    lock = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))
    registry = json.loads((ROOT / "training/seed_registry.json").read_text(encoding="utf-8"))
    firewall = EvaluationFirewall.load(ROOT / "evaluation/toolathlon_verified.manifest.json", ROOT / "training/seed_registry.json")
    for seed in registry["admitted_seeds"]:
        firewall.check_training_record(seed)
    from training.profile_admission import inspect_admission
    profile_admission = inspect_admission(ROOT, resource_backend)
    checks = []

    def check(name, passed, reason):
        checks.append({"name": name, "passed": bool(passed), "reason": reason})

    check("linux_training_host", platform.system() == "Linux", "Native verl training requires a separate suitable Linux GPU host")
    for module in ("verl", "ray", "torch", "nemo_gym", "vllm", "recipe.nemo_gym"):
        try: installed = importlib.util.find_spec(module) is not None
        except (ImportError, ModuleNotFoundError): installed = False
        check("installed_" + module.replace(".", "_"), installed, "Python module path probe; runtime import and dependency compatibility are not established")
    check("gpu_driver", shutil.which("nvidia-smi") is not None, "GPU resource not inferred from source availability")
    check("independent_training_seeds", bool(registry["admitted_seeds"]), "Candidates remain quarantined until lineage review")
    check("source_bound_task_profiles", profile_admission["passed"], "Profile contracts and admitted seeds must bind current task/configuration/implementation bytes")
    from training.source_api import report_current
    source_report = ROOT / "evidence/mainline/training_source_api.json"
    try: source_api_ready = report_current(ROOT, json.loads(source_report.read_text(encoding="utf-8")))
    except (OSError, ValueError): source_api_ready = False
    check("pinned_source_api", source_api_ready, "Fresh static signature checks required; this does not prove GPU/runtime compatibility")
    check("upstream_recipe_compatibility", lock["sources"]["verl"].get("compatibility") == "runtime_verified", "Declared recipe submodule pin currently lacks nemo_gym; no tested pair claimed")
    required = ("WORKENV_CAPTURE_KEY_FILE", "WORKENV_FIXED_EXECUTOR_ID", "WORKENV_GENERATOR_POLICY_ID",
                "WORKENV_GENERATOR_MODEL_PATH", "WORKENV_GENERATOR_TRAIN_JSONL", "WORKENV_GENERATOR_CHECKPOINT_DIR",
                "WORKENV_TRAINING_RUN_ID", "WORKENV_GENERATOR_MODEL_REVISION", "WORKENV_GENERATOR_TOKENIZER_REVISION",
                "WORKENV_FIXED_EXECUTOR_MODEL_REVISION", "WORKENV_FIXED_EXECUTOR_BASE_URL", "WORKENV_FIXED_EXECUTOR_API_KEY",
                "WORKENV_FIXED_EXECUTOR_MODEL_NAME", "NEMO_GYM_ROOT")
    for name in required:
        value = os.environ.get(name)
        valid = bool(value)
        if name.endswith(("_FILE", "_PATH", "_JSONL")) or name == "NEMO_GYM_ROOT":
            valid = valid and Path(value).exists()
        check(name, valid, "Required actual training resource; values are not included in this report")
    return {"schema_version": "skill-generator-training-preflight-v1", "training_target": "skill_generator",
            "resource_backend": resource_backend, "task_profile_admission": profile_admission,
            "ready_for_training": all(c["passed"] for c in checks), "checks": checks,
            "real_model_calls": 0, "training_updates": 0,
            "source_only_adapter": "training.verl_agent_loop.SkillGeneratorAgentLoopManager"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-ready", action="store_true")
    parser.add_argument("--resource-backend", choices=("shared", "isolated"), default="shared")
    args = parser.parse_args()
    report = inspect(args.resource_backend)
    target = ROOT / "evidence/mainline/training_preflight.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 2 if args.require_ready and not report["ready_for_training"] else 0


if __name__ == "__main__": raise SystemExit(main())
