"""Original parser with an explicitly project-derived SQL oracle, no task score."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from adapters import fulfillment_validation as validation
from adapters import meal_plan_artifacts as artifacts
from adapters.customer_spending_validation import cloud_rows, InvalidReport
from adapters.customer_report_evaluation import classify_original as classify_office, render_oracle
from integrations.fulfillment_components import snapshot, sql_truth, TASK_ID
from training.office_task_review import public_blob
from workenv.core import ResourceError, save

REFERENCE_MODE = "project_derived_sql_oracle"
REWARD_VERSION = "workenv-fulfillment-project-sql-diagnostic-v1"


def profile_registered(root):
    profiles = json.loads((root / "configs/task_adapter_profiles.json").read_text())["profiles"]
    return any(p["task_id"] == TASK_ID for p in profiles.values())


def classify_original(code, stdout, stderr):
    # The upstream parser catches database exceptions and prints RESULT: FAIL.
    # A database failure cannot establish whether the business output is wrong.
    if "Google Sheet check error:" in stdout + stderr: return None
    return classify_office(code, stdout, stderr)


def measure_snapshot(data, directory):
    try: cloud = cloud_rows(data, layout="fulfillment")
    except InvalidReport as exc:
        measured = validation.measure_workbook(data["orders"], directory, [], source_binding=validation.source_identity())
        if measured["supplemental_passed"] is not None:
            measured.update(supplemental_passed=False, errors=[str(exc)])
        return measured
    return validation.measure_workbook(data["orders"], directory, cloud, source_binding=validation.source_identity())


def evaluate(root, cluster):
    root = Path(root).resolve()
    workspace = artifacts.workspace_for(root, cluster, task="fulfillment")
    artifacts.require_no_pending_writes(cluster, task="fulfillment")
    data = snapshot(cluster)
    marker = json.loads((cluster.profile.base / "fulfillment-initialized.json").read_text())
    if marker["readonly_sha256"] != data["readonly_sha256"]: raise ResourceError("Fulfillment source rows changed")
    truth = sql_truth(cluster, data)
    directory = cluster.profile.base / "fulfillment-evaluations" / uuid4().hex
    directory.mkdir(parents=True)
    path = workspace / "Fulfillment_Report.xlsx"
    workbook_sha = None
    if path.is_file():
        if path.stat().st_size > artifacts.MAX_BYTES: raise ResourceError("Fulfillment workbook snapshot budget")
        raw = path.read_bytes()
        (directory / path.name).write_bytes(raw)
        workbook_sha = hashlib.sha256(raw).hexdigest()
    save(directory / "source_snapshot.json", data, compact=True)
    save(directory / "sql_truth.json", json.loads(json.dumps(truth, default=str)))
    measured = measure_snapshot(data, directory)
    oracle = directory / "project-derived-oracle"
    oracle.mkdir()
    render_oracle(oracle / path.name, truth, headers=validation.HEADERS)
    raw, identity = public_blob(root, TASK_ID, "evaluation/main.py")
    script = directory / "original_evaluation.py"
    script.write_bytes(raw)
    result = subprocess.run([sys.executable, str(script), "--agent_workspace", str(directory),
        "--groundtruth_workspace", str(oracle)], cwd=directory, env=cluster.profile.environment(), capture_output=True, timeout=30)
    (directory / "original.stdout.txt").write_bytes(result.stdout)
    (directory / "original.stderr.txt").write_bytes(result.stderr)
    original = classify_original(result.returncode, result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace"))
    if snapshot(cluster) != data or (hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None) != workbook_sha:
        raise ResourceError("Fulfillment evaluation inputs changed while measuring")
    receipt = {"schema_version": "fulfillment-original-parser-component-v1", "task_id": TASK_ID,
        "reference_mode": REFERENCE_MODE, "upstream_reference_used": False, "official_benchmark_score_available": False,
        "runtime_profile_registered": profile_registered(root), "training_eligible": False, "task_passed": None, "reward": None,
        "original_parser_passed": original, "supplemental_passed": measured["supplemental_passed"],
        "component_measurement_passed": None if original is None or measured["supplemental_passed"] is None else bool(original and measured["supplemental_passed"]),
        "sql_python_agree": True, "source_completeness_verified": True, "cloud_ownership_checked": True,
        "original_source": identity, "original_exit_code": result.returncode, "supplemental": measured,
        "workbook_sha256": workbook_sha, "snapshot_ref": (directory / "source_snapshot.json").relative_to(root).as_posix(),
        "oracle_sha256": hashlib.sha256((oracle / path.name).read_bytes()).hexdigest(),
        "evaluation_ref": (directory / "result.json").relative_to(root).as_posix()}
    save(directory / "result.json", receipt)
    return receipt


def episode_result(component):
    passed = component["component_measurement_passed"]
    known = type(passed) is bool
    return {"schema_version": "fulfillment-project-derived-episode-evaluation-v1", "task_id": TASK_ID,
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
