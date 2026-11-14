"""Verify standard package registration, actual Docker tools and uniform scoring."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.benchmark_tasks.store import TaskStore, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    commands = [
        [sys.executable, "-m", "unittest", "tests.test_generic_runtime", "tests.test_task_packages_verifiers", "-q"],
        [sys.executable, "scripts/benchmark_tasks.py", "register", "--benchmark", args.benchmark, "--output", str(output / "registration.json")],
        [sys.executable, "scripts/benchmark_tasks.py", "test", "--benchmark", args.benchmark, "--runtime", "--output", str(output / "smoke.json")],
    ]
    checks = []
    for index, command in enumerate(commands):
        log = output / ("check-" + str(index) + ".log")
        with log.open("x") as stream:
            result = subprocess.run(command, cwd=ROOT, env={**os.environ, "RUN_CONTAINER_CHECKS": "1"}, stdout=stream, stderr=subprocess.STDOUT)
        checks.append({"command": command, "exit_code": result.returncode, "log": log.name})
        save(output / "checks.json", checks)
        if result.returncode:
            print(log.read_text()[-12000:])
            raise RuntimeError("Acceptance check failed; full output retained at " + str(log))
    unit_log = (output / "check-0.log").read_text()
    import re
    count = re.search(r"Ran (\d+) tests", unit_log)
    if count is None or "\nOK\n" not in unit_log or "skipped=" in unit_log:
        raise ValueError("Tests must pass without skipped container checks")
    smoke = json.loads((output / "smoke.json").read_text())
    if not smoke["passed"] or smoke["model_calls"] != 0:
        raise ValueError("Runtime test outcome differs")
    shutil.copytree(smoke["run_directory"], output / "smoke-run")
    registration = json.loads((output / "registration.json").read_text())
    shutil.copy2(registration["task_set"], output / "tasks.json")
    shutil.copytree(registration["package"], output / "package")
    sources = []
    for directory in ("integrations/benchmark_tasks", "integrations/executors", "integrations/environments", "integrations/verifiers"):
        sources.extend((ROOT / directory).rglob("*.py"))
    sources += [ROOT / name for name in ("scripts/benchmark_tasks.py", "scripts/verify_generic_runtime.py",
        "tests/test_generic_runtime.py", "tests/test_task_packages_verifiers.py", "configs/api_executor.requirements.in", "configs/api_executor.requirements.lock")]
    for path in sources:
        if path.parts[-3:-1] == ("integrations", "benchmarks"):
            raise ValueError("Dataset adapter was included in generic source set")
    receipt = {"schema_version": "generic-runtime-check-v1", "passed": True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "project_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "scope": "generic_registration_model_contracts_actual_docker_mcp_services_and_evaluator",
        "tests_passed": int(count.group(1)), "benchmark": args.benchmark,
        "tasks_sha256": registration["tasks_sha256"], "package_sha256": registration["package_sha256"],
        "actual_model_calls": 0, "model_loop_driver": "test_fixture_only",
        "sample_runtime_passed": True, "sample_resources_closed": all(r["resources_closed"] for r in smoke["results"]),
        "nemo_http_deployment_verified": False, "research_benchmark_scores_produced": False, "training_admitted": False,
        "runtime_versions": {name: importlib.metadata.version(name) for name in ("openai", "anthropic", "mcp", "pydantic", "jsonschema")},
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}}
    save(output / "receipt.json", receipt)
    print(json.dumps({"passed": True, "tests": receipt["tests_passed"], "output": str(output)}))


if __name__ == "__main__":
    main()
