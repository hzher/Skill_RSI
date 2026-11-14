"""Original shipping evaluator plus exact checks, explicitly local diagnostics."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from adapters import shipping_validation as validation
from adapters import meal_plan_artifacts as artifacts
from adapters.customer_report_evaluation import render_tables
from integrations.shipping_components import snapshot, sql_truth, TASK_ID
from training.office_task_review import public_blob
from workenv.core import ResourceError, save

REFERENCE_MODE = "project_derived_sql_oracle"
REWARD_VERSION = "workenv-shipping-project-sql-diagnostic-v1"


def profile_registered(root):
    profiles = json.loads((Path(root) / "configs/task_adapter_profiles.json").read_text())["profiles"]
    return any(p["task_id"] == TASK_ID for p in profiles.values())


def classify_original(code, stdout, stderr):
    if len(stdout) + len(stderr) > 256 * 1024 or stderr.strip() or any(s in stdout for s in ("Traceback", "[FAIL] DB connect", "GT Excel exists")):
        return None
    matches = re.findall(r"^  Overall: (PASS|FAIL)$", stdout, re.MULTILINE)
    if matches == ["PASS"] and code == 0: return True
    if matches == ["FAIL"] and code == 1: return False
    return None


def evaluate(root, cluster):
    root = Path(root).resolve()
    workspace = artifacts.workspace_for(root, cluster, task="shipping")
    artifacts.require_no_pending_writes(cluster, task="shipping")
    data = snapshot(cluster)
    marker = json.loads((cluster.profile.base / "shipping-initialized.json").read_text())
    if marker["readonly_sha256"] != data["readonly_sha256"]: raise ResourceError("Read-only shipping sources changed")
    truth = sql_truth(cluster, data)
    directory = cluster.profile.base / "shipping-evaluations" / uuid4().hex
    directory.mkdir(parents=True)
    filename = "Shipping_Analysis.xlsx"
    path = workspace / filename
    workbook_sha = None
    if path.is_file():
        if path.stat().st_size > artifacts.MAX_BYTES: raise ResourceError("Shipping workbook snapshot budget")
        raw = path.read_bytes(); (directory / filename).write_bytes(raw)
        workbook_sha = hashlib.sha256(raw).hexdigest()
    save(directory / "source_snapshot.json", data, compact=True)
    save(directory / "sql_truth.json", json.loads(json.dumps(truth, default=str)))
    measured = validation.measure_workbook(data, directory)
    oracle = directory / "project-derived-oracle"; oracle.mkdir()
    render_tables(oracle / filename, validation.HEADERS, {"Zone Summary": truth["rows"], "Methods": truth["methods"]})
    raw, identity = public_blob(root, TASK_ID, "evaluation/main.py")
    script = directory / "original_evaluation.py"; script.write_bytes(raw)
    result = subprocess.run([sys.executable, str(script), "--agent_workspace", str(directory), "--groundtruth_workspace", str(oracle)],
        cwd=directory, env=cluster.profile.environment(), capture_output=True, timeout=30)
    (directory / "original.stdout.txt").write_bytes(result.stdout)
    (directory / "original.stderr.txt").write_bytes(result.stderr)
    original = classify_original(result.returncode, result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace"))
    if snapshot(cluster) != data or (hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None) != workbook_sha:
        raise ResourceError("Shipping evaluation inputs changed")
    receipt = {"schema_version": "shipping-original-parser-component-v1", "task_id": TASK_ID, "reference_mode": REFERENCE_MODE,
        "upstream_reference_used": False, "official_benchmark_score_available": False, "runtime_profile_registered": profile_registered(root),
        "task_passed": None, "reward": None, "training_eligible": False,
        "original_parser_passed": original, "supplemental_passed": measured["supplemental_passed"],
        "component_measurement_passed": None if original is None or measured["supplemental_passed"] is None else bool(original and measured["supplemental_passed"]),
        "sql_python_aggregate_agree": True, "source_completeness_verified": True,
        "original_source": identity, "original_exit_code": result.returncode, "supplemental": measured,
        "workbook_sha256": workbook_sha, "snapshot_ref": (directory / "source_snapshot.json").relative_to(root).as_posix(),
        "oracle_sha256": hashlib.sha256((oracle / filename).read_bytes()).hexdigest(),
        "evaluation_ref": (directory / "result.json").relative_to(root).as_posix()}
    save(directory / "result.json", receipt)
    return receipt


def episode_result(component):
    passed = component["component_measurement_passed"]
    known = type(passed) is bool
    return {"schema_version": "shipping-project-derived-episode-evaluation-v1", "task_id": TASK_ID,
        "evaluation_status": "completed" if known else "error", "task_passed": passed if known else None,
        "reward_version": REWARD_VERSION, "reference_mode": REFERENCE_MODE,
        "official_benchmark_score_available": False, "upstream_reference_used": False, "training_eligible": False,
        "original_parser_passed": component["original_parser_passed"], "supplemental_passed": component["supplemental_passed"],
        "component_evaluation_ref": component["evaluation_ref"]}


def evaluate_episode(root, cluster):
    component = evaluate(root, cluster)
    result = episode_result(component)
    path = Path(root) / component["evaluation_ref"]
    path = path.with_name("episode_result.json")
    save(path, result)
    return result, path
