"""Materialize only the pinned seed's public task instruction into quarantine.

No ground-truth workspace, evaluator output, Verified body, or reference solution
is read. This module has no operation that admits a candidate to training.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import os

from training.seed_contract import prompt_digest
from training.source_api import pinned_blob
from workenv.core import no_links, save

ROOT = Path(__file__).resolve().parents[1]
TASK = "wc-sales-tax-summary"


def build_candidate(instruction, source_commit, instruction_sha256, task_config_sha256):
    if not isinstance(instruction, str) or not 0 < len(instruction) <= 32000:
        raise ValueError("Invalid public task instruction")
    identity = {"source_commit": source_commit, "instruction_sha256": instruction_sha256,
                "task_config_sha256": task_config_sha256, "task_id": TASK}
    seed_sha = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    generator_prompt = ("Generate a reusable declarative skill for the following office task. Return only JSON with "
        "schema_version='skill-v1', skill_id, objective, steps (plain instruction strings), and allowed_tools. "
        "The fixed Executor will consume this skill. Available task tools: woocommerce__woo_orders_list, "
        "google_sheet__create_spreadsheet, google_sheet__list_sheets, google_sheet__rename_sheet, "
        "google_sheet__update_cells, google_sheet__get_sheet_data, write_workbook.\n\n" + instruction)
    request = {"input": [{"role": "user", "content": generator_prompt}]}
    provenance = {"source": "toolathlon_gym", "split": "candidate", "task_id": TASK, "task_family": TASK,
                  "source_commit": source_commit, "seed_sha256": seed_sha, "prompt_sha256": prompt_digest(instruction, request),
                  "ancestor_tasks": [TASK], "ancestor_families": [TASK]}
    row = {"training_target": "skill_generator", "agent_ref": {"type": "responses_api_agents", "name": "skill_generator"},
           "task_prompt": instruction, "responses_create_params": request, "provenance": provenance}
    return {"schema_version": "toolathlon-seed-candidate-v1", "candidate_status": "quarantined_not_admitted",
            "source_identity": identity, "family_alias_review": "pending_independent_review", "training_row": row,
            "reference_solutions_read": False, "verified_task_bodies_read": False}


def read_pinned_candidate(root, source):
    prefix = "tasks/finalpool/" + TASK + "/"
    instruction, instruction_meta = pinned_blob(root, source, prefix + "docs/task.md")
    config, config_meta = pinned_blob(root, source, prefix + "task_config.json")
    if max(len(instruction), len(config)) > 32000: raise ValueError("Unexpected public candidate input size")
    candidate = build_candidate(instruction.decode("utf-8"), source["commit"], hashlib.sha256(instruction).hexdigest(), hashlib.sha256(config).hexdigest())
    candidate["source_read_mode"] = "pinned_git_blob_bytes"
    candidate["git_source_files"] = {"docs/task.md": instruction_meta, "task_config.json": config_meta}
    return candidate


def preserve_candidate(root, path):
    no_links(path, root)
    if not path.is_file() or path.stat().st_size > 256 * 1024: raise ValueError("Unexpected existing candidate")
    raw = path.read_bytes()
    previous = json.loads(raw)
    permitted = {"schema_version", "candidate_status", "source_identity", "family_alias_review", "training_row",
                 "reference_solutions_read", "verified_task_bodies_read", "source_read_mode", "git_source_files"}
    if (previous.get("schema_version") != "toolathlon-seed-candidate-v1" or set(previous) - permitted
        or previous.get("candidate_status") != "quarantined_not_admitted" or previous.get("source_identity", {}).get("task_id") != TASK):
        raise ValueError("Existing candidate is not a recognized quarantined record; retained without overwrite")
    archive = root / "training/candidate_history" / (hashlib.sha256(raw).hexdigest() + ".json")
    no_links(archive, root)
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        if archive.read_bytes() != raw: raise ValueError("Existing candidate archive bytes differ; retained without overwrite")
    else:
        with archive.open("xb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
    return archive.relative_to(root).as_posix()


def main():
    lock = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))
    source = lock["sources"]["toolathlon_gym"]
    checkout = ROOT / source["checkout"]
    actual = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"], text=True).strip()
    if actual != source["commit"] or dirty:
        raise ValueError("Seed source differs from the locked upstream")
    candidate = read_pinned_candidate(ROOT, source)
    output = ROOT / "training/candidates" / (TASK + ".json")
    output.parent.mkdir(exist_ok=True)
    no_links(output, ROOT)
    encoded = (json.dumps(candidate, ensure_ascii=False, indent=2) + "\n").encode()
    archive = preserve_candidate(ROOT, output) if output.exists() and output.read_bytes() != encoded else None
    save(output, candidate)
    summary = {"schema_version": "toolathlon-quarantined-candidate-check-v1", "passed": True,
               "candidate_ref": output.relative_to(ROOT).as_posix(), "candidate_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
               "source_files_read": ["docs/task.md", "task_config.json"], "admitted_training_seeds": 0,
               "source_read_mode": candidate["source_read_mode"], "git_source_files": candidate["git_source_files"],
               "previous_candidate_archive": archive,
               "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
                   "training/candidate_seeds.py", "training/source_api.py", "configs/mainline_sources.lock.json")},
               "source_commit": actual, "reference_solutions_read": False, "verified_task_bodies_read": False}
    (ROOT / "evidence/mainline/seed_candidate.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"candidate": output.relative_to(ROOT).as_posix(), "status": candidate["candidate_status"],
                      "source_files_read": ["docs/task.md", "task_config.json"], "admitted_training_seeds": 0,
                      "source_read_mode": candidate["source_read_mode"], "previous_candidate_archive": archive}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
