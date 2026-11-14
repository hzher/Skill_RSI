"""Third original task adapter using existing resource ledgers and MCP builds."""
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil

from adapters import meal_plan_artifacts as artifacts
from adapters.order_analysis_evaluation import evaluate
from adapters.order_analysis_validation import TASK_ID, HEADERS
from integrations.calendar_component import calendar_session
from integrations.episode_cluster import require_dependents_closed
from integrations.meal_plan_services import json_bytes, MAX_ARGUMENT_BYTES, MAX_RESPONSE_BYTES
from integrations.order_analysis_components import initialize, snapshot
from workenv.core import ResourceError, no_links, save
from workenv.leases import owned_lock
from workenv.process_identity import process_identity

METHODS = {"woocommerce": ("woo_orders_list", "woo_products_list"),
           "google_calendar": ("create_event", "get_event", "update_event", "delete_event", "list_events")}


@asynccontextmanager
async def woocommerce_session(root, cluster, directory, *, shipping_locations=False):
    from adapters import toolathlon_native as native
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    node = shutil.which("node")
    if not node: raise ResourceError("Original WooCommerce MCP requires Node")
    observation = directory / "woocommerce-process.json"
    guard = directory / "woocommerce-observer.mjs"
    guard.write_text("import fs from 'node:fs';fs.writeFileSync(process.env.WORKENV_WC_OBSERVATION,JSON.stringify({pid:process.pid}));\n")
    env = cluster.profile.environment(native.env())
    if type(shipping_locations) is not bool: raise ValueError("Shipping extension flag must be explicit boolean")
    env["WORKENV_SHIPPING_READ_EXTENSIONS"] = "1" if shipping_locations else "0"
    env["WORKENV_WC_OBSERVATION"] = str(observation)
    record = {"profile": cluster.profile.record(), "closed": False}
    with owned_lock(cluster.profile.base / ".order-woocommerce.lock", root, "order_woocommerce_mcp"):
        try:
            with (directory / "woocommerce.stderr.txt").open("w", encoding="utf-8") as err:
                params = StdioServerParameters(command=node, args=["--import", guard.as_uri(), str(root / "adapters/woocommerce_native.mjs")],
                    cwd=str(directory), env=env)
                async with stdio_client(params, errlog=err) as streams:
                    async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=20)) as client:
                        await client.initialize()
                        identity = process_identity(json.loads(observation.read_text())["pid"])
                        if identity["status"] != "alive" or not identity.get("creation_id"): raise ResourceError("WooCommerce child identity is unknown")
                        record["before"] = identity
                        yield client, record
        finally:
            before = record.get("before")
            if before:
                after = record["after"] = process_identity(before["pid"])
                record["closed"] = after["status"] in ("not_found", "exited") or bool(after["status"] == "alive" and after.get("creation_id") != before["creation_id"])
            save(directory / "woocommerce-session.json", record)
            if not record["closed"]:
                save(cluster.profile.base / "woocommerce-process-unknown.json", record)
                raise ResourceError("WooCommerce dependent closure is unknown")


class OrderAnalysisResourceAdapter:
    task_id = TASK_ID

    def __init__(self, root, cluster):
        self.root, self.cluster = Path(root).resolve(), cluster
        if cluster.profile.owner != self.root: raise ResourceError("Foreign order episode")
        cluster.require_owner()
        self.build_path = self.root / "evidence/mainline/calendar_build.json"
        self.build = json.loads(self.build_path.read_text())
        self.build_sha = hashlib.sha256(self.build_path.read_bytes()).hexdigest()
        self.clients, self.children, self.schemas = {}, {}, []
        self.context, self.run, self.unknown = None, None, False

    def checkpoint(self, state):
        save(self.cluster.profile.base / "dependent_processes.json", {"schema_version": "native-episode-dependents-v1",
            "profile": self.cluster.profile.record(), "state": state, "expected_children": list(METHODS), "children": self.children})

    def guard(self):
        artifacts.workspace_for(self.root, self.cluster, task="order_analysis")
        if self.context is None or self.unknown: raise ResourceError("Order service outcome is unknown or closed")
        if hashlib.sha256(self.build_path.read_bytes()).hexdigest() != self.build_sha: raise ResourceError("Order calendar component changed")

    def new_run(self):
        if self.run is not None: raise ResourceError("Order worker cannot be reopened")
        self.run = self.cluster.profile.base / "order-task-session"
        self.run.mkdir()
        save(self.run / "run.json", {"task_id": TASK_ID, "profile": self.cluster.profile.record(), "initial_state_ref": "initial_state.json",
            "training_eligible": False, "server_model_calls": 0})
        return self.run

    async def seed(self, run):
        if run != self.run or self.context is not None: raise ResourceError("Order run was rebound")
        data = initialize(self.root, self.cluster, self.build)
        require_dependents_closed(self.cluster.profile)
        self.checkpoint("opening")
        self.context = AsyncExitStack()
        try:
            wc, self.children["woocommerce"] = await self.context.enter_async_context(woocommerce_session(self.root, self.cluster, self.run))
            self.checkpoint("opening")
            err = self.context.enter_context((self.run / "calendar.stderr.txt").open("w", encoding="utf-8"))
            cal, self.children["google_calendar"] = await self.context.enter_async_context(calendar_session(
                self.root, self.build, self.cluster, err, initialization_marker="order-analysis-initialized.json"))
            self.clients = {"woocommerce": wc, "google_calendar": cal}
            for server, client in self.clients.items():
                response = await client.list_tools()
                if not set(METHODS[server]) <= {t.name for t in response.tools}: raise ResourceError("Original order tool schema is incomplete")
                self.schemas.extend({"server": server, **t.model_dump(mode="json")} for t in response.tools if t.name in METHODS[server])
            self.checkpoint("open")
            save(self.run / "tool_schemas.json", self.schemas)
            return {"profile": self.cluster.profile.record(), "readonly_sha256": data["readonly_sha256"],
                "orders": len(data["orders"]), "products": len(data["products"]), "events": len(data["events"])}
        except BaseException:
            await self.close(run)
            raise

    def tools(self):
        self.guard()
        schema = {"type": "object", "additionalProperties": False, "required": list(HEADERS), "properties": {
            name: {"type": "array", "maxItems": 256 if name == "Status_Summary" else 4096, "items": {
                "type": "object", "additionalProperties": False, "required": list(columns), "properties": {
                    key: {"type": ["string", "null"] if key == "SKU" else "string" if key in ("Status", "Product_Name") else "number"} for key in columns}}} for name, columns in HEADERS.items()}}
        return {"tools": deepcopy(self.schemas), "artifact_tools": [
            {"name": "write_workbook", "inputSchema": schema, "description": "Write the two literal Order_Analysis.xlsx tables"},
            {"name": "read_workbook", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]}

    async def call(self, server, name, arguments, trace):
        if server not in METHODS or name not in METHODS[server] or not isinstance(arguments, dict): raise ValueError("Tool is outside the order task allowlist")
        json_bytes(arguments, MAX_ARGUMENT_BYTES)
        self.guard()
        try:
            response = await self.clients[server].call_tool(name, deepcopy(arguments))
            result = response.model_dump(mode="json")
            json_bytes(result, MAX_RESPONSE_BYTES)
            trace.append({"server": server, "name": name, "arguments": arguments, "response": result})
            return result
        except Exception as exc:
            from mcp.shared.exceptions import McpError
            if isinstance(exc, McpError) and exc.error.code == -32602: raise ValueError("Original MCP rejected tool arguments") from exc
            self.unknown = True
            raise ResourceError("Original order MCP outcome is unknown; no retry performed") from exc

    async def write(self, payload):
        self.guard()
        return artifacts.write_report(self.root, self.cluster, payload, task="order_analysis")

    async def read(self):
        self.guard()
        return artifacts.read_report(self.root, self.cluster, task="order_analysis")

    async def verify(self):
        self.guard()
        return evaluate(self.root, self.cluster)

    async def close(self, run):
        if run != self.run: raise ResourceError("Order close belongs to another run")
        if self.context is not None:
            context, self.context = self.context, None
            try: await context.aclose()
            finally: self.checkpoint("closed" if len(self.children) == 2 and all(c.get("closed") for c in self.children.values()) else "unknown")
        require_dependents_closed(self.cluster.profile)
        path = self.cluster.profile.base / "order-analysis-workspace/Order_Analysis.xlsx"
        no_links(path, self.root)
        if path.is_file():
            if path.stat().st_size > artifacts.MAX_BYTES: raise ResourceError("Order close workbook exceeds budget")
            raw = path.read_bytes()
            target = run / ("closed-workbook-" + hashlib.sha256(raw).hexdigest() + ".xlsx")
            if not target.exists(): target.write_bytes(raw)
            elif target.read_bytes() != raw: raise ResourceError("Order artifact archive changed")
        initialize(self.root, self.cluster, self.build, resetting=True)
