"""Revalidate retained provider replies through explicit transport adaptation, without inference.

Each candidate keeps its original prompt and parent. A separate diagnostic pool
three-way merges nonconflicting edits; conflicts remain on candidate branches.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.benchmark_tasks.store import save
from integrations.experience import ExperienceStore
from integrations.skill_generator.editor import SkillEditor
from integrations.skill_library import SkillLibrary


def merge_edits(current, before, after):
    merged, conflicts = current.copy(), []
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if current.get(name) not in (old, new):
            conflicts.append(name)
            continue
        if new is None:
            merged.pop(name, None)
        else:
            merged[name] = new
    return merged, conflicts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = json.loads((args.pipeline / "receipt.json").read_text())
    library = SkillLibrary(ROOT / ".runtime/skill_library")
    editor = SkillEditor(ExperienceStore(ROOT / ".runtime/experience"), library)
    rows, pool = [], {}
    for original in source["generation"]:
        index = original["index"]
        directory = args.pipeline / "generator" / f"{index:02d}"
        row = {"index": index, "task_uid": original["task_uid"], "original_status": original["status"],
               "new_model_requests": 0}
        rows.append(row)
        if not (directory / "provider_reply.json").exists():
            row["status"] = "no_provider_reply"
            continue
        try:
            request = json.loads((directory / "request.json").read_text())
            reply = json.loads((directory / "provider_reply.json").read_text())
            response = (directory / "operations.json").read_text()
            candidate = editor.apply(request, response, sampling={"kind": "api_response", "reply": reply})
            target = args.output / "candidates" / f"{index:02d}.json"
            save(target, candidate)
            if (directory / "candidate.json").exists():
                previous = json.loads((directory / "candidate.json").read_text())
                if previous["version"] != candidate["version"]:
                    raise ValueError("A formerly valid reply changed skill bytes")
            before, after = library.get(candidate["parent"]), library.get(candidate["version"])
            prior_version = library.snapshot(pool)
            pool, conflicts = merge_edits(pool, before, after)
            row.update(status="validated", candidate_id=candidate["id"], version=candidate["version"],
                parent=candidate["parent"], aliases=candidate["provenance"]["transport"]["aliases"],
                provider_response_id=reply.get("provider_response_id"), merge_conflicts=conflicts,
                pool_before=prior_version, pool_after=library.snapshot(pool),
                operation_count=len(candidate["operations"]))
        except Exception as exc:
            row.update(status="validation_failed", error=type(exc).__name__, message=str(exc)[:1500])
    inventory = []
    for name, content in sorted(pool.items()):
        filename = hashlib.sha256(name.encode()).hexdigest()[:16] + ".md"
        target = args.output / "skills" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x") as stream:
            stream.write(content)
        inventory.append({"name": name, "file": "skills/" + filename,
                          "sha256": hashlib.sha256(content.encode()).hexdigest()})
    save(args.output / "skill_inventory.json", inventory)
    receipt = {"schema_version": "workagent-skill-response-revalidation-v1", "observed_at": datetime.now(timezone.utc).isoformat(),
        "source_pipeline": str(args.pipeline), "source_run_id": source["run_id"], "results": rows,
        "validated": sum(row["status"] == "validated" for row in rows),
        "validation_failed": sum(row["status"] == "validation_failed" for row in rows),
        "namespace_adapted": sum(bool(row.get("aliases")) for row in rows),
        "diagnostic_pool_version": library.snapshot(pool), "diagnostic_pool_skill_count": len(pool),
        "merge_conflicts": sum(len(row.get("merge_conflicts", [])) for row in rows),
        "pool_is_generator_sample": False, "pool_published_to_main": False,
        "new_model_requests": 0, "training_admitted": False,
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [Path(__file__), ROOT / "integrations/skill_generator/editor.py", ROOT / "integrations/skill_generator/transport.py"]}}
    save(args.output / "receipt.json", receipt)
    print(json.dumps({key: value for key, value in receipt.items() if key not in ("results", "source_sha256")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
