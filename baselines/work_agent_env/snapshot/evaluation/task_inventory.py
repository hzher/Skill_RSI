"""Typed Verified task identities from Git trees, never from task bodies."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

from evaluation.firewall import digest
from workenv.core import contained, no_links, save
from workenv.handoff import validate_member

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = "evaluation/toolathlon_verified.tasks.json"
REQUEST_SCHEMA = "toolathlon-verified-independent-request-v2"


def pool_entries(raw, expected_entries):
    """Classify immediate Git trees and regular metadata blobs without bodies."""
    if not isinstance(raw, bytes) or len(raw) > 1024 * 1024:
        raise ValueError("Task-pool metadata exceeds its budget")
    directories, metadata, seen = set(), {}, set()
    for entry in raw.split(b"\0"):
        if not entry: continue
        header, name = entry.decode("utf-8").split("\t", 1)
        validate_member(name)
        mode, kind, checksum = header.split()
        if "/" in name or name in seen or name not in expected_entries or not re.fullmatch("[0-9a-f]{40}", checksum):
            raise ValueError("Unexpected or duplicate task-pool entry")
        seen.add(name)
        if mode == "040000" and kind == "tree": directories.add(name)
        elif mode == "100644" and kind == "blob": metadata[name] = checksum
        else: raise ValueError("Task-pool entries must be regular Git trees or blobs")
    if seen != set(expected_entries): raise ValueError("Task-pool metadata differs from the evaluation manifest")
    return directories, metadata


def validate_index(index, manifest):
    if (not isinstance(index, dict) or index.get("schema_version") != "toolathlon-verified-task-index-v1"
        or index.get("source") != "toolathlon_verified" or index.get("commit") != manifest["commit"]
        or index.get("evaluation_manifest_sha256") != digest(manifest)
        or index.get("task_bodies_loaded") is not False or index.get("solutions_loaded") is not False):
        raise ValueError("Typed task inventory is stale or belongs to another evaluation source")
    tasks, metadata = index.get("task_ids"), index.get("non_task_metadata_objects")
    if (not isinstance(tasks, list) or not 1 <= len(tasks) <= 4096 or any(not isinstance(t, str) for t in tasks)
        or not isinstance(metadata, dict) or len(metadata) > 128 or len(set(tasks)) != len(tasks)):
        raise ValueError("Typed task inventory is missing or ambiguous")
    for name in [*tasks, *metadata]:
        validate_member(name)
        if "/" in name: raise ValueError("Task inventory identities must be direct pool entries")
    if (set(tasks) & set(metadata) or set(tasks) | set(metadata) != set(manifest["task_ids"])
        or not re.fullmatch("[0-9a-f]{40}", index.get("root_git_tree", ""))
        or any(not isinstance(oid, str) or not re.fullmatch("[0-9a-f]{40}", oid) for oid in metadata.values())):
        raise ValueError("Typed task inventory does not partition its pinned manifest")
    return True


def requested_tasks(tasks, index):
    if (not isinstance(tasks, list) or not tasks or len(tasks) > 4096 or any(not isinstance(t, str) for t in tasks)
        or len(set(tasks)) != len(tasks) or not set(tasks) <= set(index["task_ids"])):
        raise ValueError("Evaluation requests must select unique task directories; metadata files are not tasks")
    return True


def load_index(root=ROOT):
    manifest_path, index_path = root / "evaluation/toolathlon_verified.manifest.json", root / INDEX_PATH
    for path in (manifest_path, index_path):
        no_links(path, root)
        if not path.is_file() or path.stat().st_size > 512 * 1024: raise ValueError("Missing or oversized typed evaluation inventory")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw = index_path.read_bytes()
    index = json.loads(raw)
    validate_index(index, manifest)
    return index, hashlib.sha256(raw).hexdigest()


def build_index(root=ROOT):
    manifest = json.loads((root / "evaluation/toolathlon_verified.manifest.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))
    source = lock["sources"]["toolathlon_verified"]
    if source["commit"] != manifest["commit"] or not re.fullmatch("[0-9a-f]{40}", source["commit"]):
        raise ValueError("Evaluation inventory and source pin differ")
    checkout = contained(root, source["checkout"])
    def git(*args): return subprocess.check_output(["git", "-C", str(checkout), *args], timeout=30, stderr=subprocess.PIPE)
    if git("rev-parse", "HEAD").decode().strip() != source["commit"]:
        raise ValueError("Verified source checkout differs from its pin")
    tree = source["commit"] + ":tasks/finalpool"
    tasks, metadata = pool_entries(git("ls-tree", "-z", tree), set(manifest["task_ids"]))
    result = {"schema_version": "toolathlon-verified-task-index-v1", "source": "toolathlon_verified",
              "commit": source["commit"], "evaluation_manifest_sha256": digest(manifest),
              "root_git_tree": git("rev-parse", tree).decode().strip(), "task_ids": sorted(tasks),
              "non_task_metadata_objects": metadata, "task_bodies_loaded": False, "solutions_loaded": False}
    validate_index(result, manifest)
    return result


def main():
    index = build_index()
    no_links(ROOT / INDEX_PATH, ROOT)
    save(ROOT / INDEX_PATH, index)
    paths = ("evaluation/task_inventory.py", INDEX_PATH, "evaluation/toolathlon_verified.manifest.json", "configs/mainline_sources.lock.json")
    report = {"schema_version": "verified-typed-inventory-observation-v1", "passed": True,
              "task_directories": len(index["task_ids"]), "non_task_entries": len(index["non_task_metadata_objects"]),
              "task_index_sha256": hashlib.sha256((ROOT / INDEX_PATH).read_bytes()).hexdigest(),
              "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths},
              "task_bodies_loaded": False, "solutions_loaded": False, "real_model_calls": 0, "training_updates": 0}
    save(ROOT / "evidence/mainline/verified_task_inventory.json", report)
    print(json.dumps({k: report[k] for k in ("passed", "task_directories", "non_task_entries", "task_bodies_loaded")}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
