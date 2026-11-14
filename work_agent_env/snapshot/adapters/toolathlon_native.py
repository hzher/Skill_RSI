"""Bounded native business verification for the pinned Toolathlon tax task.

This is a scripted diagnostic harness, not a model Agent or NeMo Gym integration.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
from zipfile import BadZipFile

import psycopg2
from openpyxl import Workbook, load_workbook
from openpyxl.utils.exceptions import InvalidFileException

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / ".upstream" / "toolathlon-gym"
TASK_ID = "wc-sales-tax-summary"
TASK = UPSTREAM / "tasks" / "finalpool" / TASK_ID
TZ = timezone(timedelta(hours=8))
TABLES = ("wc.orders", "gsheet.spreadsheets", "gsheet.sheets", "gsheet.cells", "gsheet.permissions")
_EPISODE_PROFILE = ContextVar("toolathlon_episode_resource_profile", default=None)


@contextmanager
def episode_resource(profile):
    """Bind only this async context to an already owned episode cluster."""
    from integrations.episode_cluster import EpisodeProfile, EpisodeCluster
    if not isinstance(profile, EpisodeProfile) or profile.owner != ROOT.resolve():
        raise ValueError("Episode resource belongs to a different workspace")
    EpisodeCluster(profile).require_owner()
    token = _EPISODE_PROFILE.set(profile)
    try:
        yield
    finally:
        _EPISODE_PROFILE.reset(token)


def require_run_profile(run):
    profile = _EPISODE_PROFILE.get()
    expected = profile.record() if profile is not None else None
    if json.loads((run / "run.json").read_text(encoding="utf-8")).get("resource_profile") != expected:
        raise ValueError("Original task run belongs to a different episode resource")


def now() -> str:
    return datetime.now(TZ).isoformat()


def save(path: Path, value: object) -> None:
    from workenv.core import save as save_atomic
    save_atomic(path, value)


def env() -> dict[str, str]:
    result = os.environ.copy()
    result.update({"PGHOST": "127.0.0.1", "PGPORT": "5432", "PGDATABASE": "toolathlon_gym",
                   "PGUSER": "eigent", "PGPASSWORD": "camel", "PG_HOST": "127.0.0.1",
                   "PG_PORT": "5432", "PG_DATABASE": "toolathlon_gym", "PG_USER": "eigent",
                   "PG_PASSWORD": "camel", "PGCONNECT_TIMEOUT": "3", "TOOLATHLON_ROOT": str(UPSTREAM),
                   "WOOCOMMERCE_NATIVE_ROOT": str(ROOT / ".runtime" / "woocommerce-build"),
                   "PYTHONIOENCODING": "utf-8", "WORDPRESS_SITE_URL": "http://127.0.0.1:8081",
                   "WOOCOMMERCE_CONSUMER_KEY": "local-mock", "WOOCOMMERCE_CONSUMER_SECRET": "local-mock"})
    profile = _EPISODE_PROFILE.get()
    return profile.environment(result) if profile is not None else result


@contextmanager
def database():
    profile = _EPISODE_PROFILE.get()
    if profile is not None:
        from integrations.episode_cluster import EpisodeCluster
        conn = EpisodeCluster(profile).connect()
        try: yield conn
        finally: conn.close()
        return
    marker = ROOT / ".runtime" / "cluster_owner.json"
    if not marker.exists() or json.loads(marker.read_text())["workspace"] != str(ROOT):
        raise RuntimeError("Dedicated database ownership marker missing")
    conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="toolathlon_gym",
                            user="eigent", password="camel", connect_timeout=3)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW data_directory")
            if Path(cur.fetchone()[0]).resolve() != (ROOT / ".runtime" / "pgdata").resolve():
                raise RuntimeError("Connected to a PostgreSQL cluster outside this project")
        yield conn
    finally:
        conn.close()


@contextmanager
def task_lock():
    path = ROOT / ".runtime" / "native_task.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError("A native task owns the shared database; inspect lock before retrying") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "started_at": now(), "workspace": str(ROOT)}, f)
        yield
    finally:
        path.unlink()


def run_directory(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Run IDs may contain only letters, numbers, underscores and hyphens")
    from workenv.core import no_links
    no_links(ROOT/"runs",ROOT)
    p = (ROOT / "runs" / run_id).resolve()
    if not p.is_relative_to((ROOT / "runs").resolve()):
        raise ValueError("Run path escapes workspace")
    return p


def snapshot(workspace: Path) -> dict:
    state = {}
    with database() as conn, conn.cursor() as cur:
        for table in TABLES:
            # Table names are the fixed private allowlist above, not model input.
            cur.execute(f"SELECT row_to_json(t)::text FROM {table} t")
            rows = sorted(r[0] for r in cur.fetchall())
            state[table] = {"rows": len(rows), "sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest()}
    files = {}
    for p in sorted(workspace.rglob("*")):
        if p.is_symlink():
            raise RuntimeError("Symlink in native workspace is unsupported")
        if p.is_file():
            files[str(p.relative_to(workspace))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return {"tables": state, "files": files}


def reset(run: Path, *, archive: bool = True) -> dict:
    if run_directory(run.name) != run.resolve():
        raise ValueError("Refusing reset outside owned run directory")
    require_run_profile(run)
    metadata = json.loads((run / "run.json").read_text(encoding="utf-8"))
    if metadata["workspace_owner"] != str(ROOT) or metadata["task_id"] != TASK_ID:
        raise RuntimeError("Run ownership or task identity mismatch")
    # Verify cluster ownership BEFORE upstream preprocess (which deletes shared tables).
    with database():
        pass
    workspace = run / "workspace"
    archived = None
    if workspace.exists() and archive:
        archived = run / ("workspace_before_reset_" + uuid.uuid4().hex[:8])
        if workspace.is_symlink() or not workspace.resolve().is_relative_to(run.resolve()):
            raise RuntimeError("Refusing to move an unowned workspace")
        if not archived.resolve().is_relative_to(run.resolve()):
            raise RuntimeError("Archive path escapes owned run")
        shutil.move(str(workspace), str(archived))
    workspace.mkdir(exist_ok=True)
    initial = TASK / "initial_workspace"
    if initial.exists():
        shutil.copytree(initial, workspace, dirs_exist_ok=True)
    p = subprocess.run([sys.executable, str(TASK / "preprocess" / "main.py"),
                        "--agent_workspace", str(workspace)], cwd=UPSTREAM, env=env(),
                       capture_output=True, timeout=30)
    log = run / ("reset_" + uuid.uuid4().hex[:8] + ".log")
    log.write_bytes(p.stdout + p.stderr)
    if p.returncode:
        raise RuntimeError(f"Upstream preprocess failed; see {log.name}")
    result = snapshot(workspace)
    save(run / ("reset_snapshot_" + uuid.uuid4().hex[:8] + ".json"), result)
    metadata.setdefault("reset_events", []).append({"at": now(), "archive_ref": archived.name if archived else None})
    if archived:
        metadata["artifact_refs"] = [archived.name + "/" + ref.removeprefix("workspace/")
                                     if ref.startswith("workspace/") else ref for ref in metadata["artifact_refs"]]
    save(run / "run.json", metadata)
    return result


def new_run(mode: str, run_id: str | None = None) -> Path:
    lock = json.loads((ROOT / "configs" / "upstreams.lock.json").read_text())
    head = subprocess.run(["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(UPSTREAM), "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    if head != lock["toolathlon_gym"]["commit"] or dirty:
        raise RuntimeError("Upstream commit or tracked files differ from the task pin; inspect before running")
    run_id = run_id or (datetime.now(TZ).strftime("%Y%m%dT%H%M%S") + "_" + mode + "_" + uuid.uuid4().hex[:8])
    run = run_directory(run_id)
    run.mkdir(parents=True, exist_ok=False)
    metadata = {"run_id": run_id, "workspace_owner": str(ROOT), "task_id": TASK_ID,
                "task_version": lock["toolathlon_gym"]["commit"], "code_commit": None,
                "task_family": "spreadsheet_analysis_and_business_service", "world_id": "toolathlon_gym_wc_gsheet",
                "split": "dev", "execution_mode": mode, "execution_kind": "scripted_business_check",
                "model_id": None, "policy_version": None, "harness_version": "native-diagnostic-v0.3",
                "verifier_version": {"upstream": lock["toolathlon_gym"]["commit"], "supplemental": "native-tax-v0.2"},
                "config_ref": "configs/upstreams.lock.json", "memory_mode": "disabled", "skill_version": None,
                "memory_snapshot_ref": None, "sequence_id": None, "sequence_index": None,
                "start_time": now(), "end_time": None, "termination_reason": None,
                "execution_status": "in_progress", "evaluation_status": "not_run",
                "task_passed": None, "score": None, "reward": None, "reward_version": None,
                "training_capture_ref": None, "usage": None, "trajectory_ref": "trace.json",
                "initial_state_ref": "initial_state.json", "artifact_refs": [], "error_category": None,
                "code_file_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                     for folder in (ROOT / "adapters", ROOT / "scripts", ROOT / "configs")
                                     for p in sorted(folder.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}}
    profile = _EPISODE_PROFILE.get()
    metadata["resource_profile"] = profile.record() if profile is not None else None
    save(run / "run.json", metadata)
    return run


@asynccontextmanager
async def mcp_client(name: str, run: Path):
    require_run_profile(run)
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import asyncio

    if name == "woocommerce":
        command = shutil.which("node")
        args = [str(ROOT / "adapters" / "woocommerce_native.mjs")]
    else:
        command, args = sys.executable, [str(ROOT / "adapters" / "google_sheets_native.py")]
    if not command:
        raise RuntimeError("Node is unavailable")
    with (run / (name + "_stderr.log")).open("w", encoding="utf-8") as errlog:
        params = StdioServerParameters(command=command, args=args, env=env(), cwd=str(run / "workspace"))
        async with stdio_client(params, errlog=errlog) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await asyncio.wait_for(session.initialize(), timeout=30)
                tools = await asyncio.wait_for(session.list_tools(), timeout=30)
                save(run / (name + "_tools.json"), tools.model_dump(mode="json"))
                yield session


async def call(session, name: str, arguments: dict, trace: list) -> object:
    import asyncio
    event = {"type": "tool_call", "tool": name, "arguments": arguments, "started_at": now()}
    trace.append(event)
    try:
        response = await asyncio.wait_for(session.call_tool(name, arguments), timeout=30)
        event.update({"ended_at": now(), "response": response.model_dump(mode="json")})
        if response.isError:
            raise RuntimeError(f"Tool {name} returned an error")
        texts = [c.text for c in response.content if c.type == "text"]
        if name == "list_sheets":
            # FastMCP encodes List[str] as individual plain-text blocks, not JSON.
            return texts
        return json.loads("\n".join(texts))
    except Exception as exc:
        event.update({"ended_at": now(), "error": type(exc).__name__ + ": " + str(exc)})
        raise


def monetary(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


async def correct_business_path(run: Path, trace: list) -> dict:
    orders = []
    async with mcp_client("woocommerce", run) as client:
        for status in ("completed", "processing"):
            for page in range(1, 11):
                data = await call(client, "woo_orders_list", {"status": [status], "perPage": 100, "page": page}, trace)
                if not isinstance(data, list):
                    raise RuntimeError("Order tool returned a non-list result")
                orders.extend(data)
                if len(data) < 100:
                    break
            else:
                raise RuntimeError("Order pagination exceeded diagnostic budget")
    if len({o["id"] for o in orders}) != len(orders):
        raise RuntimeError("Duplicate orders in tool pagination")
    if not orders:
        raise RuntimeError("Seed dataset unexpectedly returned zero qualifying orders; inspect tool filtering")
    states = {}
    sales, taxes = Decimal(0), Decimal(0)
    for order in orders:
        if order["status"] not in ("completed", "processing"):
            raise RuntimeError("Tool filter included non-qualifying orders")
        sale, tax = Decimal(str(order["total"])), Decimal(str(order["total_tax"]))
        sales += sale
        taxes += tax
        state = order.get("billing", {}).get("state")
        if state:
            item = states.setdefault(state, {"count": 0, "sales": Decimal(0), "tax": Decimal(0)})
            item["count"] += 1
            item["sales"] += sale
            item["tax"] += tax
    overall = [["Metric", "Value"], ["Total Orders", len(orders)], ["Total Sales", monetary(sales)],
               ["Total Tax", monetary(taxes)], ["Effective Tax Rate", monetary(taxes / sales * 100) if sales else 0]]
    workbook = Workbook()
    by_state = workbook.active
    by_state.title = "By State"
    by_state.append(["State", "Order Count", "Total Sales", "Total Tax"])
    for state, item in sorted(states.items(), key=lambda pair: (-pair[1]["tax"], pair[0])):
        by_state.append([state, item["count"], monetary(item["sales"]), monetary(item["tax"])])
    sheet = workbook.create_sheet("Overall")
    for row in overall:
        sheet.append(row)
    for row in by_state.iter_rows(min_row=2, min_col=3):
        for cell in row:
            cell.number_format = "0.00"
    dest = run / "workspace" / "Tax_Summary_Report.xlsx"
    workbook.save(dest)
    workbook.close()
    trace.append({"type": "local_python_artifact", "path": dest.name, "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()})
    async with mcp_client("google_sheet", run) as client:
        created = await call(client, "create_spreadsheet", {"title": "Tax Summary for Accounting"}, trace)
        sheet_id = created["spreadsheetId"]
        tabs = await call(client, "list_sheets", {"spreadsheet_id": sheet_id}, trace)
        if not isinstance(tabs, list) or len(tabs) != 1:
            raise RuntimeError("New shared spreadsheet must have exactly one default tab")
        await call(client, "rename_sheet", {"spreadsheet": sheet_id, "sheet": tabs[0], "new_name": "Summary"}, trace)
        await call(client, "update_cells", {"spreadsheet_id": sheet_id, "sheet": "Summary", "range": "A1:B5", "data": overall}, trace)
        readback = await call(client, "get_sheet_data", {"spreadsheet_id": sheet_id, "sheet": "Summary", "range": "A1:B5"}, trace)
    return {"qualifying_orders": len(orders), "state_count": len(states), "spreadsheet_id": sheet_id,
            "readback": readback, "overall": overall}


def supplemental_checks(workspace: Path) -> list[dict]:
    checks = []

    def check(name: str, passed: bool, detail: object = None):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    with database() as conn, conn.cursor() as cur:
        cur.execute("SELECT billing->>'state', count(*), sum(total::numeric), sum(total_tax::numeric) "
                    "FROM wc.orders WHERE status IN ('completed','processing') AND coalesce(billing->>'state','') != '' "
                    "GROUP BY billing->>'state' ORDER BY sum(total_tax::numeric) DESC")
        expected_states = {r[0]: [int(r[1]), monetary(r[2]), monetary(r[3])] for r in cur.fetchall()}
        cur.execute("SELECT count(*), sum(total::numeric), sum(total_tax::numeric) FROM wc.orders WHERE status IN ('completed','processing')")
        count, sales, tax = cur.fetchone()
        expected_overall = {"Total Orders": count, "Total Sales": monetary(sales), "Total Tax": monetary(tax),
                            "Effective Tax Rate": monetary(tax / sales * 100) if sales else 0}
        p = workspace / "Tax_Summary_Report.xlsx"
        check("artifact_exists", p.is_file())
        if p.is_file():
            try:
                wb = load_workbook(p, data_only=True)
                check("required_excel_sheets", "By State" in wb and "Overall" in wb)
                if "By State" in wb:
                    rows = list(wb["By State"].values)
                    check("state_header", rows[0] == ("State", "Order Count", "Total Sales", "Total Tax"))
                    values = {r[0]: list(r[1:4]) for r in rows[1:] if r[0]}
                    check("exact_state_set_and_no_duplicates", set(values) == set(expected_states) and len(rows)-1 == len(expected_states))
                    for state, expected in expected_states.items():
                        actual = values.get(state)
                        check("state_" + state + "_counts_sales_tax", actual is not None and len(actual) == 3 and
                              all(a is not None and abs(float(a)-float(e)) <= .01 for a,e in zip(actual,expected)))
                    tax_values = [float(r[3]) for r in rows[1:] if r[0]]
                    check("descending_total_tax", tax_values == sorted(tax_values, reverse=True))
                if "Overall" in wb:
                    rows = list(wb["Overall"].values)
                    check("overall_header", rows[0] == ("Metric", "Value"))
                    actual = {r[0]: r[1] for r in rows[1:] if r[0]}
                    check("overall_exact_metric_set", set(actual) == set(expected_overall) and len(rows) == 5)
                    for key, expected in expected_overall.items():
                        check("overall_" + key, key in actual and actual[key] is not None and abs(float(actual[key])-expected) <= .01)
                wb.close()
            except (ValueError, TypeError, KeyError, IndexError, OSError, BadZipFile, InvalidFileException) as exc:
                check("artifact_content_valid", False, str(exc))
        cur.execute("SELECT id FROM gsheet.spreadsheets WHERE title='Tax Summary for Accounting'")
        spreadsheets = cur.fetchall()
        check("exact_shared_spreadsheet_title", len(spreadsheets) == 1)
        if len(spreadsheets) == 1:
            spreadsheet_id = spreadsheets[0][0]
            cur.execute("SELECT id,title FROM gsheet.sheets WHERE spreadsheet_id=%s", (spreadsheet_id,))
            all_tabs = cur.fetchall()
            tabs = [t for t in all_tabs if t[1] == "Summary"]
            check("single_shared_summary_tab", len(all_tabs) == 1 and len(tabs) == 1)
            if len(tabs) == 1:
                cur.execute("SELECT row_index,col_index,value FROM gsheet.cells WHERE spreadsheet_id=%s AND sheet_id=%s ORDER BY row_index,col_index", (spreadsheet_id,tabs[0][0]))
                cells = {(r,c): v for r,c,v in cur.fetchall()}
                check("shared_header", cells.get((0,0)) == "Metric" and cells.get((0,1)) == "Value")
                actual = {cells.get((r,0)): cells.get((r,1)) for r in range(1,5)}
                check("shared_exact_metric_set", set(actual) == set(expected_overall))
                for key, expected in expected_overall.items():
                    value = actual.get(key)
                    try:
                        valid = value is not None and abs(float(value)-expected) <= .01
                    except (ValueError, TypeError):
                        valid = False
                    check("shared_" + key, valid)
    return checks


def evaluate(run: Path, *, inject_infrastructure_error: bool = False) -> dict:
    require_run_profile(run)
    environment = env()
    if inject_infrastructure_error:
        environment["PGHOST"] = "127.0.0.2"
    with database():
        pass
    process = subprocess.run([sys.executable, str(TASK / "evaluation" / "main.py"),
                              "--agent_workspace", str(run / "workspace")],
                             cwd=UPSTREAM, env=environment, capture_output=True, timeout=30)
    stdout, stderr = process.stdout.decode("utf-8", "replace"), process.stderr.decode("utf-8", "replace")
    (run / "evaluation_original.stdout.txt").write_text(stdout, encoding="utf-8")
    (run / "evaluation_original.stderr.txt").write_text(stderr, encoding="utf-8")
    upstream_checks = [{"name": m.group(2).strip(), "passed": m.group(1) == "PASS"}
                       for m in re.finditer(r"\[(PASS|FAIL)\] (.+)", stdout)]
    complete = "=== SUMMARY ===" in stdout and process.returncode in (0,1)
    if not complete:
        result = {"evaluation_status": "error", "task_passed": None, "score": None,
                  "error_category": "verifier_infrastructure", "upstream_exit_code": process.returncode,
                  "upstream_checks": upstream_checks, "supplemental_checks": []}
    else:
        supplemental = supplemental_checks(run / "workspace")
        upstream_passed = process.returncode == 0 and "Overall: PASS" in stdout
        passed = upstream_passed and all(c["passed"] for c in supplemental)
        result = {"evaluation_status": "completed", "task_passed": passed, "score": None,
                  "error_category": None, "upstream_passed": upstream_passed,
                  "supplemental_passed": all(c["passed"] for c in supplemental),
                  "upstream_exit_code": process.returncode,
                  "upstream_checks": upstream_checks, "supplemental_checks": supplemental,
                  "reward": None, "reward_note": "No RL reward mapping implemented"}
    save(run / "evaluation.json", result)
    return result
