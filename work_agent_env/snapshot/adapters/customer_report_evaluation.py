"""Exercise the original parser using a clearly labelled project-derived oracle.

This does not read the upstream reference workbook or yield an official score.
The spending basis is an explicit experimental input, not a resolved task fact.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from adapters import customer_spending_validation as validation
from adapters import meal_plan_artifacts as artifacts
from integrations.customer_report_components import snapshot, TASK_ID
from training.office_task_review import public_blob
from workenv.core import ResourceError, save


def render_oracle(path, truth, *, headers=None):
    headers = validation.HEADERS if headers is None else headers
    if len(headers) != 2: raise ValueError("Office oracle requires two fixed tables")
    names = tuple(headers)
    rows = {names[0]: truth["rows"], names[1]: [{"Metric": k, "Value": v} for k, v in truth["summary"].items()]}
    render_tables(path, headers, rows)


def render_tables(path, headers, rows):
    from openpyxl import Workbook
    if set(headers) != set(rows): raise ValueError("Fixed oracle tables differ")
    workbook = Workbook()
    workbook.remove(workbook.active)
    try:
        for name, values in rows.items():
            sheet = workbook.create_sheet(name)
            sheet.append(headers[name])
            for row in values:
                sheet.append([row[k] for k in headers[name]])
                for cell in sheet[sheet.max_row]:
                    if isinstance(cell.value, str): cell.data_type = "s"
        workbook.save(path)
    finally: workbook.close()


def classify_original(code, stdout, stderr):
    if len(stdout) + len(stderr) > 256 * 1024 or any(word in stdout + stderr for word in
        ("Traceback", "Cannot connect", "Error verifying", "not available", "Groundtruth not found")):
        return None
    passed, failed = stdout.count("=== RESULT: PASS ==="), stdout.count("=== RESULT: FAIL (")
    if passed == 1 and not failed and code == 0: return True
    if failed == 1 and not passed and code == 1: return False
    if code == 1 and stdout.startswith("FAIL: Agent output not found:") and not stderr: return False
    return None


def evaluate(root, cluster, *, basis):
    root = Path(root).resolve()
    workspace = artifacts.workspace_for(root, cluster, task="customer_report")
    artifacts.require_no_pending_writes(cluster, task="customer_report")
    data = snapshot(cluster)
    initialized = json.loads((cluster.profile.base / "customer-report-initialized.json").read_text())
    if initialized["readonly_sha256"] != data["readonly_sha256"]: raise ResourceError("Read-only customer data changed")
    truth = validation.expected(data["customers"], data["orders"], basis=basis)
    directory = cluster.profile.base / "customer-evaluations" / uuid4().hex
    directory.mkdir(parents=True)
    path = workspace / "WC_Customer_Report.xlsx"
    if path.is_file():
        if path.stat().st_size > artifacts.MAX_BYTES: raise ResourceError("Customer workbook snapshot budget")
        raw = path.read_bytes()
        (directory / path.name).write_bytes(raw)
        workbook_sha = hashlib.sha256(raw).hexdigest()
    else: workbook_sha = None
    save(directory / "source_snapshot.json", data, compact=True)
    measured = validation.measure_workbook(data["customers"], data["orders"], directory, data, basis=basis)
    oracle = directory / "project-derived-oracle"
    oracle.mkdir()
    render_oracle(oracle / path.name, truth)
    script_bytes, identity = public_blob(root, TASK_ID, "evaluation/main.py")
    script = directory / "original_evaluation.py"
    script.write_bytes(script_bytes)
    command = [sys.executable, str(script), "--agent_workspace", str(directory), "--groundtruth_workspace", str(oracle)]
    result = subprocess.run(command, cwd=directory, env=cluster.profile.environment(), capture_output=True, timeout=30)
    (directory / "original.stdout.txt").write_bytes(result.stdout)
    (directory / "original.stderr.txt").write_bytes(result.stderr)
    original = classify_original(result.returncode, result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace"))
    after = snapshot(cluster)
    if after != data or (hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None) != workbook_sha:
        raise ResourceError("Customer evaluation inputs changed while measuring")
    receipt = {"schema_version": "customer-original-parser-component-v1", "task_id": TASK_ID, "basis": basis,
        "original_parser_passed": original, "supplemental_passed": measured["supplemental_passed"],
        "component_measurement_passed": None if original is None or measured["supplemental_passed"] is None else bool(original and measured["supplemental_passed"]),
        "task_passed": None, "reward": None, "reference_mode": "project_derived_oracle",
        "upstream_reference_used": False, "official_benchmark_score_available": False,
        "spending_basis_approved": False, "training_eligible": False, "runtime_profile_registered": False,
        "original_source": identity, "original_exit_code": result.returncode, "supplemental": measured,
        "workbook_sha256": workbook_sha, "snapshot_ref": (directory / "source_snapshot.json").relative_to(root).as_posix(),
        "oracle_sha256": hashlib.sha256((oracle / path.name).read_bytes()).hexdigest(),
        "evaluation_ref": (directory / "result.json").relative_to(root).as_posix()}
    save(directory / "result.json", receipt)
    return receipt
