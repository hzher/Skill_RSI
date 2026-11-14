"""Inventory original Toolathlon-GYM task directories without reading answers."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

from workenv.handoff import validate_member

ROOT = Path(__file__).resolve().parents[1]


def inventory_tree(lines, verified_ids, source_commit):
    rows, seen = [], set()
    for line in lines:
        metadata, name = line.split("\t", 1)
        mode, kind, checksum = metadata.split()
        if mode != "040000" or kind != "tree" or not re.fullmatch(r"[0-9a-f]{40}", checksum):
            raise ValueError("Expected Git task-directory tree metadata")
        validate_member(name)
        if "/" in name or name in seen: raise ValueError("Task directory identity is aliased or nested")
        if name.startswith("."): continue
        seen.add(name)
        rows.append({"task_id": name, "source_tree": checksum, "exact_verified_id_overlap": name in verified_ids,
                     "admission": "quarantined_family_review_pending",
                     "adapter": "original_tax_diagnostic" if name == "wc-sales-tax-summary" else None})
    return {"schema_version": "toolathlon-gym-directory-catalog-v1", "source": "toolathlon_gym", "commit": source_commit,
            "scope": "directory_metadata_not_a_claim_of_runnable_tasks_or_training_samples", "directories": sorted(rows, key=lambda r:r["task_id"]),
            "reference_solutions_read": False, "verified_bodies_read": False, "admitted_training_seeds": 0}


def main():
    lock = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))
    source = lock["sources"]["toolathlon_gym"]
    checkout = ROOT / source["checkout"]
    head = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    if head != source["commit"]: raise ValueError("Source pin changed")
    lines = subprocess.check_output(["git", "-C", str(checkout), "ls-tree", "-d", "HEAD:tasks/finalpool"], text=True).splitlines()
    verified = json.loads((ROOT / "evaluation/toolathlon_verified.manifest.json").read_text(encoding="utf-8"))
    value = inventory_tree(lines, set(verified["task_ids"]), head)
    path = ROOT / "training/toolathlon_seed_catalog.json"
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    summary = {"schema_version": "toolathlon-seed-catalog-check-v1", "directories": len(value["directories"]),
               "exact_verified_id_overlaps": sum(r["exact_verified_id_overlap"] for r in value["directories"]),
               "admitted_training_seeds": 0, "reference_solutions_read": False, "catalog_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (ROOT / "evidence/mainline/seed_catalog.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__": raise SystemExit(main())
