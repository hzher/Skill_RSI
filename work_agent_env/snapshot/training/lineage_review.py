"""Prepare independent-review metadata and invalidate reviews after evaluation changes."""
import hashlib
import json
from pathlib import Path

from evaluation.firewall import digest

ROOT = Path(__file__).resolve().parents[1]


def evaluation_diff(previous, current, admitted_seeds):
    for manifest in (previous, current):
        if manifest.get("schema_version") != "toolathlon-verified-metadata-v1" or manifest.get("source") != "toolathlon_verified":
            raise ValueError("Expected independent Verified metadata")
    added_tasks = set(current["task_ids"]) - set(previous["task_ids"])
    removed_tasks = set(previous["task_ids"]) - set(current["task_ids"])
    added_families = set(current["family_roots"]) - set(previous["family_roots"])
    changed = digest(previous) != digest(current)
    conflicts = []
    for seed in admitted_seeds:
        tasks = {seed["task_id"], *seed.get("ancestor_tasks", [])}
        families = {seed["task_family"], *seed.get("ancestor_families", [])}
        if tasks & set(current["task_ids"]) or families & set(current["family_roots"]):
            conflicts.append(seed["seed_sha256"])
    return {"schema_version": "verified-lineage-diff-v1", "manifest_changed": changed,
            "upstream_pin_changed": previous["commit"] != current["commit"],
            "added_tasks": sorted(added_tasks), "removed_tasks": sorted(removed_tasks), "added_families": sorted(added_families),
            "conflicting_admitted_seed_hashes": sorted(conflicts), "all_admitted_reviews_require_refresh": changed,
            "automatic_admissions": 0}


def prepare_review(candidate, manifest, blob_audit=None):
    if candidate.get("schema_version") != "toolathlon-seed-candidate-v1" or candidate.get("candidate_status") != "quarantined_not_admitted":
        raise ValueError("Only quarantined seed candidates are supported")
    provenance = candidate["training_row"]["provenance"]
    if provenance.get("source") != "toolathlon_gym" or provenance.get("split") != "candidate":
        raise ValueError("Review input must remain outside training")
    blob_matches = None
    if blob_audit is not None:
        if (blob_audit.get("schema_version") != "task-prompt-git-blob-comparison-v1" or blob_audit.get("evaluation_manifest_sha256") != digest(manifest)
            or blob_audit.get("gym_commit") != provenance["source_commit"] or blob_audit.get("verified_commit") != manifest["commit"]):
            raise ValueError("Prompt-object comparison is stale or belongs to another source")
        blob_matches = blob_audit["exact_prompt_blob_overlaps"].get(provenance["task_id"], [])
    return {"schema_version": "toolathlon-seed-review-material-v1", "status": "exact_prompt_overlap_not_admissible" if blob_matches else "lineage_review_pending",
            "candidate_source": candidate["source_identity"], "evaluation_manifest_sha256": digest(manifest),
            "exact_id_overlap": bool({provenance["task_id"], *provenance.get("ancestor_tasks", [])} & set(manifest["task_ids"])),
            "family_root_overlap": bool({provenance["task_family"], *provenance.get("ancestor_families", [])} & set(manifest["family_roots"])),
            "alias_lineage_review_complete": False, "training_admission": False,
            "exact_prompt_blob_overlap_tasks": blob_matches,
            "source_instruction_or_solution_included": False}


def main():
    manifest = json.loads((ROOT / "evaluation/toolathlon_verified.manifest.json").read_text(encoding="utf-8"))
    rows = []
    blob_path = ROOT / "evidence/mainline/prompt_blob_overlap.json"
    blob_audit = json.loads(blob_path.read_text(encoding="utf-8")) if blob_path.exists() else None
    for path in sorted((ROOT / "training/candidates").glob("*.json")):
        if path.is_symlink() or path.stat().st_size > 256 * 1024: raise ValueError("Unexpected candidate input")
        row = prepare_review(json.loads(path.read_text(encoding="utf-8")), manifest, blob_audit)
        row["candidate_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(row)
    output = ROOT / "evidence/mainline/seed_review_materials.json"
    output.write_text(json.dumps({"schema_version": "seed-review-materials-v1", "reviews": rows, "automatic_admissions": 0}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"review_materials": len(rows), "automatic_admissions": 0, "source_bodies_loaded": False}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
