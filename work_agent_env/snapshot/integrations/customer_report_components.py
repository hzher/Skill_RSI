"""Owned local customer-report components, not a registered NeMo task adapter."""
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from adapters import meal_plan_artifacts as artifacts
from adapters import customer_spending_validation as validation
from integrations.episode_cluster import require_dependents_closed
from integrations.order_analysis_resource import woocommerce_session
from integrations.meal_plan_services import json_bytes, MAX_ARGUMENT_BYTES, MAX_RESPONSE_BYTES
from training.office_task_review import public_blob
from workenv.core import ResourceError, no_links, save
from workenv.process_identity import process_identity

TASK_ID = validation.TASK_ID
METHODS = {"woocommerce": ("woo_customers_list", "woo_orders_list"),
           "google_sheet": ("create_spreadsheet", "list_sheets", "rename_sheet", "update_cells", "get_sheet_data")}
GSHEET_TABLES = ("spreadsheets", "sheets", "cells", "permissions", "folders")


def snapshot(cluster, *, include_shipping=False):
    cluster.require_owner()
    conn = cluster.connect()
    result = {}
    try:
        conn.rollback()
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with conn.cursor() as cur:
            names = ("customers", "orders", "shipping_zones", "shipping_zone_locations", "shipping_zone_methods") if include_shipping else ("customers", "orders")
            for key, table in [(n, "wc." + n) for n in names] + [(n, "gsheet." + n) for n in GSHEET_TABLES]:
                cur.execute("SELECT count(*) FROM " + table)
                count = cur.fetchone()[0]
                if not 0 <= count <= 100000: raise ResourceError("Customer snapshot row budget exceeded")
                cur.execute("SELECT row_to_json(t)::text FROM " + table + " t ORDER BY row_to_json(t)::text LIMIT 100001")
                values = [json.loads(row[0]) for row in cur.fetchall()]
                if len(values) != count: raise ResourceError("Customer snapshot is incomplete")
                result[key] = values
    finally:
        conn.close()
    result["readonly_sha256"] = hashlib.sha256(json.dumps({n: result[n] for n in names}, sort_keys=True).encode()).hexdigest()
    return result


def initialize(root, cluster, *, task_id=TASK_ID):
    root = Path(root).resolve()
    layouts = {TASK_ID: ("customer-report-workspace", "customer-report-initialized.json", "customer-preprocess-"),
               "wc-order-fulfillment-tracker": ("fulfillment-workspace", "fulfillment-initialized.json", "fulfillment-preprocess-"),
               "wc-shipping-zone-gsheet": ("shipping-workspace", "shipping-initialized.json", "shipping-preprocess-")}
    if task_id not in layouts: raise ValueError("Unsupported office initialization")
    folder, marker_name, prefix = layouts[task_id]
    if cluster.profile.owner != root or not cluster.started: raise ResourceError("Customer episode is not owned and active")
    before = snapshot(cluster, include_shipping=task_id == "wc-shipping-zone-gsheet")
    workspace = cluster.profile.base / folder
    no_links(workspace, root)
    workspace.mkdir(exist_ok=True)
    directory = cluster.profile.base / (prefix + uuid4().hex)
    directory.mkdir()
    raw, identity = public_blob(root, task_id, "preprocess/main.py")
    script = directory / "original_preprocess.py"
    script.write_bytes(raw)
    marker = cluster.profile.base / marker_name
    save(marker, {"state": "initializing", "task_id": task_id, "profile": cluster.profile.record()})
    result = subprocess.run([sys.executable, str(script), "--agent_workspace", str(workspace)],
        cwd=directory, env=cluster.profile.environment(), capture_output=True, timeout=25)
    (directory / "stdout.txt").write_bytes(result.stdout)
    (directory / "stderr.txt").write_bytes(result.stderr)
    after = snapshot(cluster, include_shipping=task_id == "wc-shipping-zone-gsheet")
    if result.returncode or any(after[n] for n in GSHEET_TABLES) or after["readonly_sha256"] != before["readonly_sha256"]:
        raise ResourceError("Original customer reset failed or changed read-only sources")
    save(marker, {"state": "ready", "task_id": task_id, "profile": cluster.profile.record(),
        "preprocess_source": identity, "readonly_sha256": before["readonly_sha256"]})
    return after


@asynccontextmanager
async def sheets_session(root, cluster, directory):
    from adapters import toolathlon_native as native
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    observer = directory / "sheets-observer.py"
    observation = directory / "sheets-process.json"
    observer.write_text("import os,json,runpy\nfrom pathlib import Path\n"
        "Path(os.environ['WORKENV_SHEETS_OBSERVATION']).write_text(json.dumps({'pid':os.getpid()}))\n"
        "runpy.run_path(os.environ['WORKENV_SHEETS_WRAPPER'],run_name='__main__')\n")
    env = cluster.profile.environment(native.env())
    env.update(WORKENV_SHEETS_OBSERVATION=str(observation), WORKENV_SHEETS_WRAPPER=str(root / "adapters/google_sheets_native.py"))
    record = {"profile": cluster.profile.record(), "closed": False}
    try:
        with (directory / "sheets.stderr.txt").open("w", encoding="utf-8") as err:
            params = StdioServerParameters(command=sys.executable, args=[str(observer)], env=env, cwd=str(directory))
            async with stdio_client(params, errlog=err) as streams:
                async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=20)) as client:
                    await client.initialize()
                    before = process_identity(json.loads(observation.read_text())["pid"])
                    if before["status"] != "alive" or not before.get("creation_id"): raise ResourceError("Sheets process identity unknown")
                    record["before"] = before
                    yield client, record
    finally:
        before = record.get("before")
        if before:
            after = record["after"] = process_identity(before["pid"])
            record["closed"] = after["status"] in ("exited", "not_found") or bool(after["status"] == "alive" and after.get("creation_id") and after["creation_id"] != before["creation_id"])
        save(directory / "sheets-session.json", record)
        if not record["closed"]: raise ResourceError("Original Sheets closure is unknown")


class CustomerReportComponents:
    """Local component owner; task routing, NeMo and rewards are deliberately absent."""
    task_id, methods, artifact_task, run_name = TASK_ID, METHODS, "customer_report", "customer-components-run"
    shipping_locations = False
    def __init__(self, root, cluster):
        self.root, self.cluster = Path(root).resolve(), cluster
        if cluster.profile.owner != self.root: raise ResourceError("Foreign customer resource")
        self.run = cluster.profile.base / self.run_name
        self.clients, self.children, self.journal, self.schemas = {}, {}, [], []
        self.unknown, self.opened = False, False
        self.entered = False

    def checkpoint(self, state):
        save(self.cluster.profile.base / "dependent_processes.json", {"schema_version": "native-episode-dependents-v1",
            "profile": self.cluster.profile.record(), "state": state, "expected_children": list(self.methods), "children": self.children})

    @asynccontextmanager
    async def open(self):
        if self.entered or self.run.exists(): raise ResourceError("Office component cannot be reopened")
        self.entered = True
        require_dependents_closed(self.cluster.profile)
        self.run.mkdir()
        initialize(self.root, self.cluster, task_id=self.task_id)
        self.checkpoint("opening")
        try:
            async with AsyncExitStack() as stack:
                wc, self.children["woocommerce"] = await stack.enter_async_context(woocommerce_session(self.root, self.cluster, self.run, shipping_locations=self.shipping_locations))
                self.checkpoint("opening")
                sheet, self.children["google_sheet"] = await stack.enter_async_context(sheets_session(self.root, self.cluster, self.run))
                self.clients = {"woocommerce": wc, "google_sheet": sheet}
                for name, client in self.clients.items():
                    response = await client.list_tools()
                    if not set(self.methods[name]) <= {t.name for t in response.tools}: raise ResourceError("Office original tool schema missing")
                    self.schemas.extend({"server": name, **t.model_dump(mode="json")} for t in response.tools if t.name in self.methods[name])
                save(self.run / "tool_schemas.json", self.schemas)
                self.opened = True
                self.checkpoint("open")
                yield self
        finally:
            self.opened = False
            self.checkpoint("closed" if len(self.children) == 2 and all(c.get("closed") for c in self.children.values()) else "unknown")
            save(self.run / "tool_journal.json", self.journal)
            require_dependents_closed(self.cluster.profile)

    async def call(self, server, name, arguments):
        if not self.opened or self.unknown: raise ResourceError("Customer component is closed or unknown")
        if server not in self.methods or name not in self.methods[server]: raise ValueError("Original method is not allowed for office components")
        json_bytes(arguments, MAX_ARGUMENT_BYTES)
        artifacts.workspace_for(self.root, self.cluster, task=self.artifact_task)
        event = {"server": server, "name": name, "arguments": arguments}
        self.journal.append(event)
        try:
            result = (await self.clients[server].call_tool(name, arguments)).model_dump(mode="json")
            json_bytes(result, MAX_RESPONSE_BYTES)
            event["response"] = result
            return result
        except Exception as exc:
            self.unknown = True
            event.update(error_type=type(exc).__name__, outcome_unknown=True)
            raise ResourceError("Customer MCP outcome unknown; no operation retried") from exc
