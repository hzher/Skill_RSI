"""Accept interfaces using container tools and existing real API traces; no paid requests."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.benchmark_tasks.store import save
from integrations.experience import ExperienceStore
from integrations.skill_generator.editor import SkillEditor
from integrations.skill_library import SkillLibrary
from scripts.skills import ingest_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, "-m", "unittest", "tests.test_experience_skills", "tests.test_generic_runtime",
               "tests.test_executor_diagnostics", "tests.test_task_packages_verifiers",
               "tests.test_skill_generator_contract", "tests.test_skill_registry_and_usage"]
    with (args.output / "tests.log").open("x") as stream:
        result = subprocess.run(command, cwd=ROOT, env={**os.environ, "RUN_CONTAINER_CHECKS": "1"},
                                stdout=stream, stderr=subprocess.STDOUT)
    text = (args.output / "tests.log").read_text()
    if result.returncode or "skipped=" in text:
        print(text[-8000:])
        raise RuntimeError("Acceptance tests failed or container checks were skipped")
    store, library = ExperienceStore(ROOT / ".runtime/experience"), SkillLibrary(ROOT / ".runtime/skill_library")
    imported = ingest_run(store, args.run_directory)
    save(args.output / "imports.json", imported)
    editor = SkillEditor(store, library)
    checks = []
    for item in imported["results"]:
        request = editor.prepare(item["id"], generator_policy_id="interface-acceptance-no-sampling")
        view = request["view"]
        checks.append({"experience_id": item["id"], "task_uid": view["task_uid"], "request_id": request["id"],
                       "steps": len(view["steps"]), "result": view["result"], "truncation": view["truncation"],
                       "artifacts": len(view["artifacts"]), "sampling_performed": False})
    import re
    count = int(re.search(r"Ran (\d+) tests", text).group(1))
    receipt = {"schema_version": "workagent-experience-skills-acceptance-v1", "passed": True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "project_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "tests_passed": count, "container_checks_skipped": False,
        "historical_run_id": args.run_directory.name, "imported_experiences": len(checks),
        "prepared_generator_requests": len(checks), "original_measured": sum(c["result"]["status"] == "measured" for c in checks),
        "original_passed": sum(c["result"]["passed"] is True for c in checks),
        "unknown_scores": sum(c["result"]["status"] == "unknown" for c in checks),
        "actual_new_model_requests": 0, "generator_sampling_verified": False, "training_admitted": False,
        "upstream_revision": "3c039efeafd89400115426a9a7af19b58601f0be", "checks": checks,
        "command": command, "source_sha256": {}}
    sources = []
    for directory in ("experience", "skill_generator", "skill_library", "executors"):
        sources += list((ROOT / "integrations" / directory).rglob("*.py"))
    sources += [Path(__file__), ROOT / "scripts/skills.py", ROOT / "scripts/benchmark_tasks.py", ROOT / "tests/test_experience_skills.py"]
    receipt["source_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    save(args.output / "receipt.json", receipt)
    print(json.dumps({k: v for k, v in receipt.items() if k not in ("checks", "source_sha256", "command")}))


if __name__ == "__main__":
    main()
