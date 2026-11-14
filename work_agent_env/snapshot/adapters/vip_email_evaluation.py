"""Original database evaluator plus exact, source-bound VIP component checks."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from adapters import vip_email_validation as validation
from adapters.customer_spending_validation import InvalidReport, integer
from integrations.email_components import snapshot, initialize as initialize_email
from integrations.vip_email_components import check_customers, sql_truth, save_snapshot
from training.email_task_review import BASE, public_blob, TASK_ID
from workenv.core import ResourceError, save


def cloud_snapshot(data):
    office = data["office"]
    parents = office["spreadsheets"]
    if not parents: raise InvalidReport("vip_cloud_spreadsheet_missing")
    if len(parents) != 1 or parents[0]["title"] != validation.TITLE: raise InvalidReport("vip_cloud_parent_not_unique")
    parent = parents[0]; sheets = office["sheets"]
    if len(sheets) != 2 or {s["title"] for s in sheets} != set(validation.HEADERS) or any(s["spreadsheet_id"] != parent["id"] for s in sheets):
        raise InvalidReport("vip_cloud_tabs_not_unique_or_owned")
    grids = {s["id"]: {} for s in sheets}
    for cell in office["cells"]:
        if cell["spreadsheet_id"] != parent["id"] or cell["sheet_id"] not in grids: raise InvalidReport("vip_foreign_cloud_cell")
        row, col = integer(cell["row_index"], InvalidReport),integer(cell["col_index"], InvalidReport)
        if row > 10 or col > 4 or (row,col) in grids[cell["sheet_id"]] or cell.get("formula"):
            raise InvalidReport("vip_cloud_cell_duplicate_formula_or_budget")
        grids[cell["sheet_id"]][row,col] = cell["value"]
    result = {"title": parent["title"], "sheets": []}
    for sheet in sheets:
        columns = validation.HEADERS[sheet["title"]]; grid = grids[sheet["id"]]
        indices = sorted({r for r,c in grid})
        if not indices or indices != list(range(indices[-1]+1)) or set(grid) != {(r,c) for r in indices for c in range(len(columns))}:
            raise InvalidReport("vip_cloud_sparse_or_extra_cells")
        result["sheets"].append({"title": sheet["title"], "headers": [grid[0,c] for c in range(len(columns))],
            "rows": [dict(zip(columns,(grid[r,c] for c in range(len(columns))))) for r in indices[1:]]})
    return result


def supplement(data):
    try: cloud = cloud_snapshot(data)
    except InvalidReport as exc:
        result = validation.measure(data["office"]["customers"], {"title": "invalid", "sheets": []}, data["email"]["messages"], data["email"]["sent_log"], source_binding=validation.source_identity())
        if result["supplemental_passed"] is False: result["errors"] = [str(exc)]
        return result
    return validation.measure(data["office"]["customers"],cloud,data["email"]["messages"],data["email"]["sent_log"],source_binding=validation.source_identity())


def classify_original(code, stdout, stderr):
    if stderr or len(stdout) > 256*1024: return None
    matched = re.search(r"=== SUMMARY ===\n\s*Passed: (\d+)\n\s*Failed: (\d+)\n\s*Overall: (PASS|FAIL)\s*$",stdout)
    if not matched: return None
    passed, failed = int(matched[1]),int(matched[2])
    if passed != stdout.count("[PASS]") or failed != stdout.count("[FAIL]") or passed+failed < 1: return None
    if not failed and matched[3] == "PASS" and code == 0: return True
    if failed and matched[3] == "FAIL" and code == 1: return False
    return None


def evaluate(controller):
    controller.guard()
    root, cluster = controller.root, controller.cluster
    data = snapshot(cluster)
    source_path = controller.run / "original_customers.json"
    if not source_path.is_file(): raise ResourceError("VIP original customer inventory has not been verified")
    source = json.loads(source_path.read_text())
    check_customers(source["customers"],data)
    if source["readonly_sha256"] != data["office"]["readonly_sha256"]: raise ResourceError("VIP original customer snapshot changed")
    sql = sql_truth(cluster,data)
    directory = cluster.profile.base / "vip-evaluations" / uuid4().hex; directory.mkdir(parents=True)
    save_snapshot(root,directory/'source_snapshot.json',data,controller.run/'initial_snapshot.json'); save(directory / "sql_truth.json",sql)
    script, identity = public_blob(root, BASE + "evaluation/main.py")
    (directory / "original_evaluation.py").write_bytes(script)
    result = subprocess.run([sys.executable,str(directory / "original_evaluation.py"),"--agent_workspace",str(directory)],
        cwd=directory,env=cluster.profile.environment(),capture_output=True,timeout=30)
    (directory / "original.stdout.txt").write_bytes(result.stdout); (directory / "original.stderr.txt").write_bytes(result.stderr)
    original = classify_original(result.returncode,result.stdout.decode(errors="replace"),result.stderr.decode(errors="replace"))
    supplemental = supplement(data)
    controller.guard()
    if snapshot(cluster) != data: raise ResourceError("VIP evaluator changed its database input")
    record = {"schema_version": "vip-original-database-component-measurement-v1", "task_id": TASK_ID,
        "profile": cluster.profile.record(), "original_source": identity, "original_exit_code": result.returncode,
        "original_passed": original, "supplemental": supplemental,
        "component_measurement_passed": None if original is None or supplemental["supplemental_passed"] is None else bool(original and supplemental["supplemental_passed"]),
        "source_completeness_verified": True, "owned_database_snapshot_verified": True,
        "snapshot_ref": (directory / "source_snapshot.json").relative_to(root).as_posix(),
        "original_customer_inventory_ref": source_path.relative_to(root).as_posix(),
        "original_customer_inventory_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "evaluation_ref": (directory / "result.json").relative_to(root).as_posix(),
        "reference_mode": "original_database_evaluator_with_project_supplement", "upstream_reference_used": False,
        "official_benchmark_score_available": False, "runtime_task_activated": False, "task_passed": None, "reward": None,
        "training_eligible": False, "external_messages_sent": 0}
    save(directory / "result.json",record)
    return record


def reset(root, cluster):
    journal = cluster.profile.base / "vip-components-run/requests.json"
    if journal.exists():
        value = json.loads(journal.read_text())
        if value.get("profile") != cluster.profile.record() or any(e.get("state") != "committed" for e in value.get("entries",{}).values()):
            raise ResourceError("VIP request outcome unresolved; reset refused")
    return initialize_email(root,cluster)
