"""Check the unmerged PR26 candidate using real images and no model calls."""
from __future__ import annotations

import ast
import dataclasses
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "sources/continual-learning-bench-pr26"
BASE = ROOT / "sources/continual-learning-bench"
OUT = ROOT / "reports/protocol-review"


def run(args, *, check=True, timeout=150):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check, timeout=timeout)


def execute(container, text, *, check=True):
    return run(["docker", "exec", "-w", "/testbed", container, "bash", "-lc", text], check=check)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def function_ast(path, name):
    node = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.dump(node, include_attributes=False)


def write(value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "isolation-check.json").write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    sys.path.insert(0, str(CANDIDATE))
    from src.tasks.codebase_adaptation.generic_runtime import initialize_generic_pr_container
    from src.tasks.codebase_adaptation.sanitized_runtime import initialize_sanitized_actor_container
    from src.tasks.codebase_adaptation.evaluator import evaluate_submission

    candidate_sha = run(["git", "-C", str(CANDIDATE), "rev-parse", "HEAD"]).stdout.strip()
    assert candidate_sha == "a6f5ede32b4c6c13ccc4c793b3e9b9d7aebe5617"
    assert "continual-learning-bench-pr26" in str(sys.modules[initialize_sanitized_actor_container.__module__].__file__)
    report = {
        "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "candidate_sha": candidate_sha,
        "candidate_status": "unmerged_upstream_pr26_including_pr25",
        "base_sha": "5f8c50eb1e84b2eda2ef4faff757dfc812a0ea26",
        "model_calls": 0,
        "new_benchmark_rollouts": 0,
        "actors": [],
        "evaluator_probes": [],
    }
    data = Path("data/codebase_adaptation/final-dataset.jsonl")
    report["dataset_bytes_identical"] = sha(BASE / data) == sha(CANDIDATE / data)
    report["scorer_wrapper_bytes_identical"] = sha(BASE / "src/tasks/codebase_adaptation/evaluator.py") == sha(CANDIDATE / "src/tasks/codebase_adaptation/evaluator.py")
    generic = Path("src/tasks/codebase_adaptation/generic_runtime.py")
    report["evaluation_function_ast_identical"] = function_ast(BASE / generic, "evaluate_generic_pr_submission") == function_ast(CANDIDATE / generic, "evaluate_generic_pr_submission")
    schedules = Path("src/tasks/codebase_adaptation/schedules")
    report["schedules_bytes_identical"] = all((BASE / schedules / p.name).is_file() and sha(BASE / schedules / p.name) == sha(p) for p in (CANDIDATE / schedules).glob("*.json"))
    assert all(report[k] for k in ("dataset_bytes_identical", "scorer_wrapper_bytes_identical", "evaluation_function_ast_identical", "schedules_bytes_identical"))
    rows = {row["instance_id"]: row for line in (CANDIDATE / data).read_text().splitlines() if (row := json.loads(line))}
    for instance_id, module in (("jazzband__tablib-534", "tablib"), ("jd__tenacity-597", "tenacity")):
        row = rows[instance_id]
        container = None
        try:
            container = run(["docker", "run", "-d", "--rm", "--network=none", "--name", "rsi-isolation-actor-" + uuid.uuid4().hex[:8], "-w", "/testbed", row["image_name"], "sleep", "10m"]).stdout.strip()
            base_tree = execute(container, "git rev-parse " + row["base_commit"] + "^{tree}").stdout.strip()
            inherited_head = execute(container, "git rev-parse HEAD").stdout.strip()
            initialize_sanitized_actor_container(container, row)
            actor_tree = execute(container, "git rev-parse HEAD^{tree}").stdout.strip()
            count = execute(container, "git rev-list --all --count").stdout.strip()
            old_commit = execute(container, "git cat-file -e " + inherited_head + "^{commit}", check=False)
            imports = execute(container, "python -c 'import " + module + "; print(\"IMPORT_OK\")'", check=False)
            probe = {
                "instance_id": instance_id,
                "image": row["image_name"],
                "base_commit": row["base_commit"],
                "base_tree": base_tree,
                "actor_tree": actor_tree,
                "base_tree_preserved_exactly": actor_tree == base_tree,
                "git_commit_count": int(count),
                "inherited_commit_inaccessible": old_commit.returncode != 0,
                "remote_list_empty": not execute(container, "git remote").stdout.strip(),
                "working_tree_clean_including_ignored": not execute(container, "git status --porcelain=v1 --untracked-files=all --ignored").stdout.strip(),
                "package_import_passed": imports.returncode == 0 and "IMPORT_OK" in imports.stdout,
                "actor_head": execute(container, "git rev-parse HEAD").stdout.strip(),
                "official_test_patch_absent": actor_tree == base_tree,
            }
            assert probe["git_commit_count"] == 1 and all(probe[k] for k in ("base_tree_preserved_exactly", "inherited_commit_inaccessible", "remote_list_empty", "working_tree_clean_including_ignored", "package_import_passed", "official_test_patch_absent")), probe
            report["actors"].append(probe)
            write(report)
        finally:
            if container:
                run(["docker", "rm", "-f", container], check=False)
        container = None
        try:
            container = run(["docker", "run", "-d", "--rm", "--network=none", "--name", "rsi-isolation-grader-" + uuid.uuid4().hex[:8], "-w", "/testbed", row["image_name"], "sleep", "10m"]).stdout.strip()
            initialize_generic_pr_container(container, row)
            changed = execute(container, "git diff --quiet", check=False)
            probe = {"instance_id": instance_id, "official_test_patch_applied_in_evaluator": changed.returncode == 1, "evaluator_retains_base_commit": execute(container, "git rev-parse HEAD").stdout.strip() == row["base_commit"]}
            assert all(probe[k] for k in ("official_test_patch_applied_in_evaluator", "evaluator_retains_base_commit"))
            report["evaluator_probes"].append(probe)
            write(report)
        finally:
            if container:
                run(["docker", "rm", "-f", container], check=False)
    archived = ROOT / "runs/smoke/smoke_20261112T160554+0800_772c97b8/audit/submitted-worktree.patch"
    result = evaluate_submission(model_patch=archived.read_text(), instance=rows["jazzband__tablib-534"])
    report["prior_archived_patch_regrade_on_candidate"] = {"patch_sha256": sha(archived), "result": dataclasses.asdict(result), "is_new_model_result": False}
    assert result.success
    report["passed"] = True
    write(report)
    print(json.dumps({"passed": True, "actor_images_checked": len(report["actors"]), "evaluator_images_checked": len(report["evaluator_probes"]), "scorer_and_dataset_unchanged": True, "model_calls": 0, "report": str(OUT / "isolation-check.json")}))


if __name__ == "__main__":
    main()
