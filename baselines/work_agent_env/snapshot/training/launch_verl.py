"""Build or launch the actual verl command only after explicit readiness checks."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from training.preflight import ROOT, inspect
from training.source_api import report_current


def pinned_python_paths():
    """Expose separate pinned checkouts as verl and recipe.nemo_gym packages."""
    lock = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))
    checkouts = {}
    for name in ("verl", "verl_recipe"):
        source = lock["sources"][name]
        checkout = (ROOT / source["checkout"]).resolve(strict=True)
        actual = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                                         timeout=30, stderr=subprocess.PIPE).decode().strip()
        if actual != source["commit"]:
            raise RuntimeError(f"{name} checkout differs from the pinned commit")
        checkouts[name] = checkout
    report = json.loads((ROOT / "evidence/mainline/training_source_api.json").read_text(encoding="utf-8"))
    if not report_current(ROOT, report):
        raise RuntimeError("Pinned source API receipt is absent or stale")
    namespace = ROOT / ".runtime/verl-pythonpath"
    namespace.mkdir(parents=True, exist_ok=True)
    recipe_link = namespace / "recipe"
    if recipe_link.is_symlink():
        if recipe_link.resolve(strict=True) != checkouts["verl_recipe"]:
            raise RuntimeError("Recipe import path points to a different checkout")
    elif recipe_link.exists():
        raise RuntimeError("Recipe import path is occupied")
    else:
        recipe_link.symlink_to(checkouts["verl_recipe"], target_is_directory=True)
    return [str(ROOT), str(namespace), str(checkouts["verl"])]


def command(resource_backend="shared"):
    if resource_backend not in ("shared", "isolated"): raise ValueError("Unknown Generator resource backend")
    args = [sys.executable, "-m", "verl.trainer.main_ppo", "--config-path", str(ROOT / "training/configs"),
            "--config-name", "skill_generator"]
    if resource_backend == "isolated":
        args.append("actor_rollout_ref.rollout.agent.agent_loop_config_path=" + str(ROOT / "training/configs/nemo_generator_isolated.yaml"))
    return args


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Requires real installed GPU/model/data/capture dependencies")
    parser.add_argument("--resource-backend", choices=("shared", "isolated"), default="shared")
    args = parser.parse_args()
    source_paths = []
    if args.execute:
        try: source_paths = pinned_python_paths()
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(f"Pinned verl source setup failed: {exc}", file=sys.stderr)
            return 2
        sys.path[:0] = source_paths
    report = inspect(args.resource_backend)
    print(json.dumps({"ready_for_training": report["ready_for_training"], "resource_backend": args.resource_backend, "command": command(args.resource_backend),
                      "status": "launch_requested" if args.execute else "command_only_not_executed"}, indent=2))
    if not args.execute:
        return 0
    if not report["ready_for_training"]:
        print("Training blocked by preflight; no optimizer was launched", file=sys.stderr)
        return 2
    env = os.environ.copy()
    env.update(WORKENV_PROJECT_ROOT=str(ROOT),
               WORKENV_GENERATOR_RESOURCE_BACKEND=args.resource_backend,
               WORKENV_VERIFIED_MANIFEST=str(ROOT / "evaluation/toolathlon_verified.manifest.json"),
               WORKENV_TRAINING_REGISTRY=str(ROOT / "training/seed_registry.json"))
    env["NEMO_GYM_EXTRA_ROOTS"] = str(ROOT) + (os.pathsep + env["NEMO_GYM_EXTRA_ROOTS"] if env.get("NEMO_GYM_EXTRA_ROOTS") else "")
    env["PYTHONPATH"] = os.pathsep.join(source_paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return subprocess.run(command(args.resource_backend), cwd=ROOT, env=env).returncode


if __name__ == "__main__": raise SystemExit(main())
