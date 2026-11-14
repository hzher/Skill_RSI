"""Compare Git prompt-object identities without reading held-out task bodies."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

from evaluation.firewall import digest
from evaluation.task_inventory import pool_entries
from workenv.core import contained
from workenv.handoff import validate_member

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "tasks/finalpool/"
PROMPT_PATHS = ("docs/task.md", "task.md", "initial_workspace/task.md")


def prompt_entries(raw, expected_tasks):
    if not isinstance(raw, bytes) or len(raw) > 4 * 1024 * 1024:
        raise ValueError("Git prompt metadata exceeds its byte budget")
    rows, paths = {}, set()
    entries = raw.split(b"\0")
    if len(entries) > 20000: raise ValueError("Git object inventory exceeds its member budget")
    for entry in entries:
        if not entry: continue
        header, name = entry.decode("utf-8").split("\t", 1)
        if not name.endswith(("/docs/task.md", "/task.md")): continue
        validate_member(name)
        mode, kind, checksum = header.split()
        if mode != "100644" or kind != "blob" or not re.fullmatch("[0-9a-f]{40}", checksum):
            raise ValueError("Prompt metadata must refer to a regular Git blob")
        if not name.startswith(PREFIX): raise ValueError("Prompt reference is outside the task pool")
        task, remaining = name[len(PREFIX):].split("/", 1)
        if remaining not in PROMPT_PATHS or task not in expected_tasks or name in paths:
            raise ValueError("Prompt task identity is missing, nested, duplicated or aliased")
        paths.add(name)
        rows.setdefault(task, []).append({"path": name, "git_blob": checksum})
    if set(rows) != set(expected_tasks): raise ValueError("Prompt inventory does not cover the pinned task metadata")
    result = {}
    for task, objects in rows.items():
        objects.sort(key=lambda row: PROMPT_PATHS.index(row["path"][len(PREFIX) + len(task) + 1:]))
        result[task] = objects[0]
        if len(objects) > 1: result[task]["additional_prompt_objects"] = objects[1:]
    return result


def compare_prompts(gym, verified):
    by_blob = {}
    def objects(row): return [row, *row.get("additional_prompt_objects", [])]
    for task, row in verified.items():
        for obj in objects(row): by_blob.setdefault(obj["git_blob"], set()).add(task)
    overlaps = {}
    for task, row in gym.items():
        matches = set().union(*(by_blob.get(obj["git_blob"], set()) for obj in objects(row)))
        if matches: overlaps[task] = sorted(matches)
    return {"exact_prompt_blob_overlaps": overlaps, "overlapping_gym_tasks": len(overlaps),
            "semantic_independence": "unknown_requires_family_alias_review", "automatic_admissions": 0,
            "verified_task_bodies_read": False, "reference_solutions_read": False}


def main():
    lock = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "evaluation/toolathlon_verified.manifest.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "training/toolathlon_seed_catalog.json").read_text(encoding="utf-8"))
    expected = {"toolathlon_gym": {r["task_id"] for r in catalog["directories"]}, "toolathlon_verified": set(manifest["task_ids"])}
    inventories, non_task_metadata = {}, {}
    for name in expected:
        source = lock["sources"][name]
        if not re.fullmatch("[0-9a-f]{40}", source["commit"]): raise ValueError("Invalid pinned source commit")
        checkout = contained(ROOT, source["checkout"])
        head = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True, timeout=10).strip()
        if head != source["commit"]: raise ValueError("Prompt source checkout differs from its pin")
        if name == "toolathlon_verified":
            pool = subprocess.check_output(["git", "-C", str(checkout), "ls-tree", "-z", head + ":tasks/finalpool"], timeout=30)
            expected[name], non_task_metadata = pool_entries(pool, expected[name])
        raw = subprocess.check_output(["git", "-C", str(checkout), "ls-tree", "-rz", "--full-tree", head, "tasks/finalpool"], timeout=30)
        inventories[name] = prompt_entries(raw, expected[name])
    if manifest["commit"] != lock["sources"]["toolathlon_verified"]["commit"] or catalog["commit"] != lock["sources"]["toolathlon_gym"]["commit"]:
        raise ValueError("Task metadata manifests differ from the compared source pins")
    result = {"schema_version": "task-prompt-git-blob-comparison-v1", **compare_prompts(inventories["toolathlon_gym"], inventories["toolathlon_verified"]),
              "gym_commit": catalog["commit"], "verified_commit": manifest["commit"], "evaluation_manifest_sha256": digest(manifest),
              "gym_prompt_count": len(inventories["toolathlon_gym"]), "verified_prompt_count": len(inventories["toolathlon_verified"]),
              "verified_non_task_metadata_objects": non_task_metadata,
              "gym_prompt_objects": inventories["toolathlon_gym"], "verified_prompt_objects": inventories["toolathlon_verified"],
              "scope": "Git tree/object metadata only; no task or solution body reads", "real_model_calls": 0, "training_updates": 0,
              "source_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in (
                  "training/prompt_blob_overlap.py", "evaluation/task_inventory.py", "configs/mainline_sources.lock.json", "evaluation/toolathlon_verified.manifest.json", "training/toolathlon_seed_catalog.json")}}
    target = ROOT / "evidence/mainline/prompt_blob_overlap.json"
    target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("gym_prompt_count", "verified_prompt_count", "overlapping_gym_tasks", "semantic_independence", "automatic_admissions")}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
