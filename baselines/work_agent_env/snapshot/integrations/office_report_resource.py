"""Shared component context and fixed artifact lifecycle for office reports."""
from copy import deepcopy
import hashlib
from pathlib import Path

from adapters import meal_plan_artifacts as artifacts
from integrations.episode_cluster import require_dependents_closed
from workenv.core import ResourceError, no_links, save

class OfficeReportResourceAdapter:

    def __init__(self, root, cluster):
        self.root, self.cluster = Path(root).resolve(), cluster
        if cluster.profile.owner != self.root: raise ResourceError("Foreign office report episode")
        cluster.require_owner()
        self.components = self.component_type(self.root, cluster)
        self.context, self.run = None, None

    def new_run(self):
        if self.run is not None: raise ResourceError("Office report worker cannot be reopened")
        self.run = self.cluster.profile.base / self.run_name
        self.run.mkdir()
        save(self.run / "run.json", {"task_id": self.task_id, "profile": self.cluster.profile.record(),
            "initial_state_ref": "initial_state.json", "training_eligible": False, "server_model_calls": 0})
        return self.run

    async def seed(self, run):
        if run != self.run or self.context is not None: raise ResourceError("Office report run was rebound")
        self.context = self.components.open()
        try:
            await self.context.__aenter__()
            if {(t["server"],t["name"]) for t in self.components.schemas} != {(s,n) for s,names in self.methods.items() for n in names}:
                raise ResourceError("Office report component method inventory differs")
            data = self.source_snapshot()
            return {"profile": self.cluster.profile.record(), "orders": len(data["orders"]),
                    "readonly_sha256": data["readonly_sha256"], "reference_mode": "project_derived_sql_oracle"}
        except BaseException:
            context = self.context
            try:
                if self.components.opened:
                    await context.__aexit__(None, None, None)
            finally:
                self.context = None
            raise

    def guard(self):
        artifacts.workspace_for(self.root, self.cluster, task=self.artifact_task)
        if self.context is None or not self.components.opened or self.components.unknown:
            raise ResourceError("Office report services are closed or unknown")

    def tools(self):
        self.guard()
        schema = {"type": "object", "additionalProperties": False, "required": list(self.headers), "properties": {
            table: {"type": "array", "maxItems": self.row_limits[table], "items": {
                "type": "object", "additionalProperties": False, "required": list(columns), "properties": {
                    key: {"type": "string" if key in self.text_columns else "number"} for key in columns}}}
            for table, columns in self.headers.items()}}
        return {"tools": deepcopy(self.components.schemas), "artifact_tools": [
            {"name": "write_workbook", "inputSchema": schema, "description": "Write the fixed report tables"},
            {"name": "read_workbook", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]}

    async def call(self, server, name, arguments, trace):
        if server not in self.methods or name not in self.methods[server] or not isinstance(arguments, dict):
            raise ValueError("Tool is outside the office report allowlist")
        self.guard()
        arguments = deepcopy(arguments)
        result = await self.components.call(server, name, arguments)
        trace.append({"server": server, "name": name, "arguments": arguments, "response": result})
        return result

    async def write(self, payload):
        self.guard()
        return artifacts.write_report(self.root, self.cluster, payload, task=self.artifact_task)

    async def read(self):
        self.guard()
        return artifacts.read_report(self.root, self.cluster, task=self.artifact_task)

    async def verify(self):
        self.guard()
        return self.evaluate_episode()

    async def close(self, run):
        if run != self.run: raise ResourceError("Office report close belongs to another run")
        if self.context is not None:
            context, self.context = self.context, None
            await context.__aexit__(None, None, None)
        require_dependents_closed(self.cluster.profile)
        path = self.cluster.profile.base / self.workbook_ref
        no_links(path, self.root)
        if path.is_file():
            if path.stat().st_size > artifacts.MAX_BYTES: raise ResourceError("Office report close workbook exceeds budget")
            raw = path.read_bytes()
            target = run / ("closed-workbook-" + hashlib.sha256(raw).hexdigest() + ".xlsx")
            artifacts.preserve_bytes(target, raw, self.root)
        self.initialize()
