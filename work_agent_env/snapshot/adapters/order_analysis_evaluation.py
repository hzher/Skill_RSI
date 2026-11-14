"""Run original and supplemental order evaluators against complete owned data."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from adapters.meal_plan_artifacts import workspace_for, require_no_pending_writes
from adapters.order_analysis_validation import contract_identity, measure_workbook
from integrations.order_analysis_components import public_source, snapshot
from workenv.core import ResourceError, no_links, save


ORIGINAL_CHECK_NAMES = {
    "Excel file exists": "workbook_file_exists",
    "Excel file readable": "workbook_readable",
    "Sheet 'Status_Summary' exists": "workbook_status_sheet_exists",
    "Sheet 'Low_Stock_Products' exists": "workbook_low_stock_sheet_exists",
    "Status_Summary row count": "status_row_count",
    "Status_Summary sorted alphabetically": "status_sort_order",
    "Low_Stock_Products row count": "low_stock_row_count",
    "Low_Stock sorted by Stock_Quantity ascending": "low_stock_sort_order",
    "DB connection for gcal check": "calendar_database_connection",
    "At least one calendar event exists": "calendar_event_exists",
    "Event title contains 'Restock Planning Meeting'": "calendar_meeting_title",
    "Event is on 2026-03-10": "calendar_meeting_day",
    "Event description is not empty": "calendar_description_present",
    "Event description references low-stock context": "calendar_description_low_stock_context",
    "Restock Planning Meeting event found": "calendar_meeting_found",
}


def original_checks(stdout):
    """Return only fixed check categories and booleans, never log details or row IDs."""
    if not isinstance(stdout,str) or len(stdout)>256*1024:return []
    checks={}
    for line in stdout.splitlines():
        match=re.fullmatch(r"\s*\[(PASS|FAIL)\] ([^:\r\n]+)(?::[^\r\n]*)?",line)
        if match is None or match[2] not in ORIGINAL_CHECK_NAMES:continue
        name=ORIGINAL_CHECK_NAMES[match[2]]
        checks[name]=checks.get(name,True) and match[1]=='PASS'
    return [{'name':name,'passed':passed} for name,passed in checks.items()]


def classify_original(code, stdout, stderr):
    if (type(code) is not int or code not in (0, 1) or len(stdout) + len(stderr) > 256*1024
        or "Traceback" in stderr or "Falling back" in stdout or "WARNING:" in stdout
        or "INFO: Using dynamically computed expected values from PostgreSQL" not in stdout): return None
    match = re.findall(r"^\s*Overall: (PASS|FAIL)\s*$", stdout, re.MULTILINE)
    if match == ["PASS"] and code == 0: return True
    if match == ["FAIL"] and code == 1: return False
    return None


def evaluate(root, cluster):
    root = Path(root).resolve()
    output = cluster.profile.base / "order-evaluations" / uuid4().hex
    output.mkdir(parents=True)
    upstream, supplemental, proc, source = None, {}, None, None
    workbook_sha = None
    error = None
    upstream_checks = []
    try:
        workspace = workspace_for(root, cluster, task="order_analysis")
        require_no_pending_writes(cluster, task="order_analysis")
        data = snapshot(cluster)
        marker = json.loads((cluster.profile.base / "order-analysis-initialized.json").read_text())
        if data["readonly_sha256"] != marker["readonly_sha256"]: raise ResourceError("Read-only order data changed")
        save(output / "source_snapshot.json", data, compact=True)
        workbook = workspace / "Order_Analysis.xlsx"
        no_links(workbook, root)
        if workbook.exists():
            if not workbook.is_file() or workbook.stat().st_size > 1024*1024: raise ResourceError("Order workbook snapshot exceeds budget")
            raw_workbook = workbook.read_bytes()
            workbook_sha = hashlib.sha256(raw_workbook).hexdigest()
            (output / "Order_Analysis.xlsx").write_bytes(raw_workbook)
        supplemental = measure_workbook(data["orders"], data["products"], data["events"], output, source_binding=contract_identity())
        if supplemental["evaluation_status"] != "completed": raise ResourceError("Supplemental order verifier is unavailable")
        raw, source = public_source(root, "evaluation/main.py")
        script = output / "original_evaluation.py"
        script.write_bytes(raw)
        empty = output / "empty-reference-workspace"
        empty.mkdir()  # Explicitly empty; no reference solution can be read on fallback.
        proc = subprocess.run([sys.executable, str(script), "--agent_workspace", str(workspace), "--groundtruth_workspace", str(empty)],
            cwd=output, env=cluster.profile.environment(), capture_output=True, timeout=25)
        (output / "original.stdout.txt").write_bytes(proc.stdout)
        (output / "original.stderr.txt").write_bytes(proc.stderr)
        upstream = classify_original(proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"))
        if type(upstream) is bool:upstream_checks=original_checks(proc.stdout.decode("utf-8", "replace"))
        if snapshot(cluster)["readonly_sha256"] != data["readonly_sha256"]: raise ResourceError("Evaluator changed read-only data")
        if (hashlib.sha256(workbook.read_bytes()).hexdigest() if workbook.is_file() else None) != workbook_sha:
            raise ResourceError("Order workbook changed during evaluation")
    except Exception as exc:
        upstream, error = None, type(exc).__name__
        upstream_checks = []
    valid = type(upstream) is bool and supplemental.get("evaluation_status") == "completed" and type(supplemental.get("supplemental_passed")) is bool
    result = {"schema_version": "original-order-analysis-evaluation-v1", "at": datetime.now(timezone.utc).isoformat(),
        "evaluation_status": "completed" if valid else "error", "task_passed": upstream and supplemental["supplemental_passed"] if valid else None,
        "upstream_passed": upstream, "supplemental_passed": supplemental.get("supplemental_passed"), "supplemental": supplemental,
        "upstream_checks": upstream_checks,
        "error_category": None if valid else "verifier_infrastructure", "infrastructure_error_type": error,
        "profile": cluster.profile.record(), "original_evaluator_source": source, "original_evaluator_executed": proc is not None,
        "upstream_exit_code": proc.returncode if proc else None, "source_snapshot_ref": (output / "source_snapshot.json").relative_to(root).as_posix(),
        "snapshot_completeness_verified": valid, "reference_solutions_read": False, "verified_task_bodies_read": False,
        "workbook_sha256": workbook_sha, "workbook_missing": workbook_sha is None,
        "real_model_calls": 0, "training_updates": 0, "training_eligible": False, "reward": None,
        "source_sha256": {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in (
            "adapters/order_analysis_evaluation.py", "adapters/order_analysis_validation.py", "adapters/meal_plan_artifacts.py",
            "integrations/order_analysis_components.py", "integrations/episode_cluster.py")}}
    save(output / "evaluation.json", result)
    return result, output / "evaluation.json"
