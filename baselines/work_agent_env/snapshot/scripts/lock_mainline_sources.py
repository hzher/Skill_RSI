"""Record exact upstream roles and Verified tree identities without loading answers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluation.firewall import digest


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(ROOT / path), *args], text=True, encoding="utf-8").strip()


def write(path, value):
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    sources = {}
    for name, path, role in [
        ("nemo_gym", ".upstream/nemo-gym", "main_environment_runtime"),
        ("toolathlon_gym", ".upstream/toolathlon-gym", "training_seed_candidates"),
        ("verl_recipe", ".upstream/verl-recipe", "skill_generator_training_integration"),
        ("toolathlon_verified", ".upstream/toolathlon-verified", "independent_evaluation_metadata_only"),
    ]:
        if git(path, "status", "--porcelain", "--untracked-files=no") and name != "toolathlon_verified":
            raise RuntimeError("Tracked upstream changes: " + name)
        sources[name] = {"url": git(path, "remote", "get-url", "origin"), "commit": git(path, "rev-parse", "HEAD"), "checkout": path, "role": role}
    recipe = ROOT / ".upstream/verl-recipe/nemo_gym/REQUIRED_VERL.txt"
    declared = dict(line.split("=", 1) for line in recipe.read_text(encoding="utf-8").splitlines() if "=" in line)
    # Upstream's declared submodule pin currently lacks the nemo_gym directory.
    tree = git(".upstream/verl-recipe", "ls-tree", "--name-only", declared["RECIPE_SUBMODULE_COMMIT"], "nemo_gym")
    sources["verl"] = {"url": declared["UPSTREAM"], "commit": declared["VERL_COMMIT"], "role": "post_train_skill_generator",
                       "install_status": "source_pin_only", "recipe_submodule_declared": declared["RECIPE_SUBMODULE_COMMIT"],
                       "declared_submodule_contains_nemo_recipe": bool(tree), "compatibility": "not_runtime_verified",
                       "metadata_sha256": hashlib.sha256(recipe.read_bytes()).hexdigest()}
    if (ROOT / ".upstream/verl/.git").exists():
        actual = git(".upstream/verl", "rev-parse", "HEAD")
        if actual != declared["VERL_COMMIT"]:
            raise RuntimeError("verl source differs from recipe-declared core pin")
        sources["verl"].update(checkout=".upstream/verl", source_commit_verified=True)
    write("configs/mainline_sources.lock.json", {"schema_version": "workagent-mainline-sources-v1", "sources": sources})
    path = ".upstream/toolathlon-verified"
    ids = git(path, "ls-tree", "--name-only", "HEAD:tasks/finalpool").splitlines()
    manifest = {"schema_version": "toolathlon-verified-metadata-v1", "source": "toolathlon_verified",
                "url": sources["toolathlon_verified"]["url"], "commit": sources["toolathlon_verified"]["commit"],
                "task_ids": ids, "family_roots": ids, "inventory_kind": "repository_tree_not_claimed_benchmark_task_count",
                "task_bodies_loaded": False, "solutions_loaded": False,
                "family_review": "task_id_roots_only; aliases require a separate independent lineage review"}
    write("evaluation/toolathlon_verified.manifest.json", manifest)
    registry_path = ROOT / "training/seed_registry.json"
    if registry_path.exists():
        current = json.loads(registry_path.read_text(encoding="utf-8"))
        if current.get("admitted_seeds"):
            raise RuntimeError("Do not overwrite an admitted training registry; review source pin changes explicitly")
    write("training/seed_registry.json", {"schema_version": "toolathlon-training-seeds-v1", "evaluation_manifest_sha256": digest(manifest),
          "admitted_seeds": [], "quarantined_candidates": [{"source": "toolathlon_gym", "task_id": "wc-sales-tax-summary",
          "reason": "diagnostic-only until task-family/alias independence review; exact ID alone is insufficient",
          "exact_id_overlap": "wc-sales-tax-summary" in ids}]})
    print(json.dumps({"source_roles": len(sources), "verified_tree_entries": len(ids), "admitted_training_seeds": 0,
                      "recipe_compatibility_gap": not bool(tree), "solutions_loaded": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
