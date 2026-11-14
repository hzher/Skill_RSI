"""Meal-plan business adapter for the existing isolated resource dispatcher.

The dispatcher retains the ledger, binding, request cache and lease authority.
All async MCP contexts stay in the worker's single serving task.
"""
from copy import deepcopy
import hashlib
from pathlib import Path

from integrations.calendar_component import initialize
from integrations.meal_plan_services import MealPlanServices, TASK_ID, components
from workenv.core import ResourceError, no_links, save

WORKBOOK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["meal_plan", "shopping_list"],
    "properties": {"meal_plan": {"type": "array", "maxItems": 128, "items": {"type": "object", "additionalProperties": False,
        "required": ["day", "meal_type", "recipe_name", "prep_time"], "properties": {
            "day": {"type": "string", "maxLength": 512}, "meal_type": {"type": "string", "maxLength": 512},
            "recipe_name": {"type": "string", "maxLength": 512}, "recipe_id": {"type": "string", "maxLength": 512},
            "prep_time": {"type": "number", "minimum": -10**12, "maximum": 10**12}}}},
        "shopping_list": {"type": "array", "maxItems": 4096, "items": {"type": "object", "additionalProperties": False,
            "required": ["ingredient", "quantity"], "properties": {"ingredient": {"type": "string", "maxLength": 512},
                "quantity": {"anyOf": [{"type": "string", "maxLength": 512}, {"type": "number", "minimum": -10**12, "maximum": 10**12}]}}}}}}


class MealPlanResourceAdapter:
    task_id = TASK_ID

    def __init__(self, root, cluster):
        self.root, self.cluster = Path(root).resolve(), cluster
        cluster.require_owner()
        if cluster.profile.owner != self.root: raise ResourceError("Meal resource belongs to another workspace")
        self.receipts, self.bindings, _ = components(self.root)
        self.context, self.services, self.run = None, None, None

    def new_run(self):
        if self.run is not None: raise ResourceError("An isolated meal worker accepts one task run")
        self.run = self.cluster.profile.base / "meal-task-session"
        no_links(self.run, self.root)
        self.run.mkdir()
        save(self.run / "run.json", {"task_id": TASK_ID, "profile": self.cluster.profile.record(),
            "initial_state_ref": "initial_state.json", "component_receipt_sha256": self.bindings,
            "training_eligible": False, "server_model_calls": 0})
        return self.run

    async def seed(self, run):
        if run != self.run or self.context is not None: raise ResourceError("Meal worker run was rebound")
        initialize(self.root, self.receipts["calendar"], self.cluster)
        self.services = MealPlanServices(self.root, self.cluster)
        context = self.services.open()
        await context.__aenter__()
        self.context = context
        return {"profile": self.cluster.profile.record(), "calendar_events": 0, "workbook_present": False,
            "component_receipt_sha256": self.bindings}

    def tools(self):
        return {"tools": self.services.tools(), "artifact_tools": [
            {"name": "write_workbook", "inputSchema": deepcopy(WORKBOOK_SCHEMA),
             "description": "Write the two task-specific meal plan and shopping list tables"},
            {"name": "read_workbook", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
             "description": "Read this episode's Weekly_Meal_Plan.xlsx bytes"}]}

    async def call(self, server, name, arguments, trace):
        result = await self.services.call(server, name, arguments)
        trace.append({"type": "original_mcp", "server": server, "name": name, "arguments": deepcopy(arguments), "response": result})
        return result

    async def write(self, payload): return await self.services.write_workbook(payload)
    async def read(self): return await self.services.read_workbook()
    async def verify(self): return await self.services.verify()

    async def close(self, run):
        if run != self.run: raise ResourceError("Meal close belongs to another run")
        if self.context is not None:
            context, self.context = self.context, None
            try: await context.__aexit__(None, None, None)
            except BaseException:
                # The bundle retains child ownership/unknown flags. A failed
                # exit must never be reported as a clean resource release.
                save(run / "services-closed.json", self.services.record())
                raise
            save(run / "services-closed.json", self.services.record())
        from integrations.episode_cluster import require_dependents_closed
        require_dependents_closed(self.cluster.profile)
        workbook = self.cluster.profile.base / "calendar-workspace/Weekly_Meal_Plan.xlsx"
        no_links(workbook, self.root)
        if workbook.is_file():
            if workbook.stat().st_size > 1024 * 1024: raise ResourceError("Meal close artifact exceeds budget")
            raw = workbook.read_bytes()
            archive = run / ("closed-workbook-" + hashlib.sha256(raw).hexdigest() + ".xlsx")
            no_links(archive, self.root)
            if archive.exists():
                if archive.read_bytes() != raw: raise ResourceError("Meal close archive differs")
            else:
                with archive.open("xb") as stream: stream.write(raw)
            save(run / "closed-artifact.json", {"ref": archive.relative_to(self.root).as_posix(), "sha256": hashlib.sha256(raw).hexdigest()})
        # Reset only after all original MCP children have closed, retaining the
        # final workbook above. The outer worker alone owns PG shutdown.
        initialize(self.root, self.receipts["calendar"], self.cluster)
