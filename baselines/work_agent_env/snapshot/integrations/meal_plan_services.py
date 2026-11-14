"""Episode-bound business services for the original meal-plan task.

The caller owns PostgreSQL initialization and the authoritative resource ledger.
This bundle never resets an episode, retries a request, or runs an agent loop.
"""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from adapters.meal_plan_artifacts import read_report, workspace_for, write_report
from adapters.meal_plan_evaluation import evaluate
from integrations.calendar_component import calendar_session, verify_build
from integrations.episode_cluster import require_dependents_closed
from integrations.howtocook_component import NETWORK_GUARD, verify_component
from integrations.resource_transport import MAX_TOOL_RESPONSE_BYTES, wire_json
from training.recipe_snapshot import verify_snapshot
from workenv.core import ResourceError, no_links, save
from workenv.leases import owned_lock
from workenv.process_identity import process_identity

TASK_ID = "howtocook-meal-plan-gcal"
METHODS = {"howtocook": ("mcp_howtocook_getAllRecipes", "mcp_howtocook_getRecipeById"),
           "google_calendar": ("create_event", "get_event", "update_event", "delete_event", "list_events")}
COMPONENT_FILES = {"recipe": "evidence/mainline/howtocook_build.json",
                   "calendar": "evidence/mainline/calendar_build.json",
                   "snapshot": "evidence/mainline/howtocook_recipe_snapshot.json"}
MAX_CALLS = 128
MAX_ARGUMENT_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = MAX_TOOL_RESPONSE_BYTES


def json_bytes(value, limit):
    try:
        raw = wire_json(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Business service payload must be finite JSON") from exc
    if len(raw) > limit: raise ValueError("Business service payload exceeds its byte budget")
    return raw


def components(root):
    root = Path(root).resolve()
    receipts, hashes = {}, {}
    for key, name in COMPONENT_FILES.items():
        path = root / name
        no_links(path, root)
        if path.stat().st_size > 256 * 1024: raise ValueError("Component receipt exceeds budget")
        raw = path.read_bytes()
        receipts[key], hashes[name] = json.loads(raw), hashlib.sha256(raw).hexdigest()
    recipe_base = verify_component(root, receipts["recipe"])
    verify_build(root, receipts["calendar"])
    verify_snapshot(root, receipts["snapshot"])
    pin = json.loads((root / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]["toolathlon_gym"]["commit"]
    if (any(value.get("source_commit") != pin for value in receipts.values())
        or receipts["recipe"].get("recipe_sha256") != receipts["snapshot"]["sha256"]):
        raise ValueError("Meal-plan components have different pinned sources or recipe data")
    return receipts, hashes, recipe_base


@asynccontextmanager
async def recipe_session(root, base, cluster, directory, errlog):
    """One original read-only recipe process, observed and locked per resource."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    node = shutil.which("node")
    if not node: raise ResourceError("Node is required for the original recipe MCP")
    unknown = cluster.profile.base / "recipe-process-unknown.json"
    no_links(unknown, root)
    if unknown.exists(): raise ResourceError("Previous recipe child shutdown is unknown")
    guard, observation = directory / "network_guard.mjs", directory / "recipe-observation.json"
    guard.write_text(NETWORK_GUARD, encoding="utf-8")
    env = {**os.environ, "WORKENV_RECIPE_GUARD": str(observation)}
    params = StdioServerParameters(command=str(Path(node).resolve()),
        args=["--import", guard.as_uri(), str(base / "build/index.js"), "--transport", "stdio"], env=env, cwd=str(base))
    record = {"profile": cluster.profile.record(), "observation_ref": observation.relative_to(root).as_posix()}
    with owned_lock(cluster.profile.base / ".recipe.lock", root, "meal_recipe_episode_mcp"):
        try:
            async with stdio_client(params, errlog=errlog) as streams:
                async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=20)) as client:
                    await client.initialize()
                    observed = json.loads(observation.read_text(encoding="utf-8"))
                    identity = process_identity(observed["pid"])
                    if identity["status"] != "alive" or not identity.get("creation_id"):
                        raise ResourceError("Cannot confirm original recipe child identity")
                    record["before"] = identity
                    record["closed"] = False
                    save(directory / "recipe-session.json", record)
                    yield client, record
        finally:
            before = record.get("before")
            if before:
                after = process_identity(before["pid"])
                record["after"] = after
                record["closed"] = after["status"] in ("not_found", "exited") or bool(after.get("creation_id") and after["creation_id"] != before["creation_id"])
            else:
                record["closed"] = False
            if observation.is_file():
                observed = json.loads(observation.read_text(encoding="utf-8"))
                record["instrumented_network_calls"] = observed.get("instrumented_network_calls")
            save(directory / "recipe-session.json", record)
            if record["closed"] is not True:
                save(unknown, record)
                raise ResourceError("Recipe child shutdown is unknown; preserve this episode")
            if record.get("instrumented_network_calls") != 0:
                raise ResourceError("Recipe component attempted an instrumented network request")


class MealPlanServices:
    """Fixed task methods over two original MCP clients and bounded artifacts."""
    def __init__(self, root, cluster, *, task_id=TASK_ID):
        self.root, self.cluster = Path(root).resolve(), cluster
        if task_id != TASK_ID: raise ValueError("Unsupported meal-plan task identity")
        workspace_for(self.root, cluster)
        self.profile = deepcopy(cluster.profile.record())
        self.receipts, self.bindings, self.recipe_base = components(self.root)
        self.clients, self.schemas, self.sessions = {}, [], {}
        self.state, self.calls = "new", 0
        self.outcome_unknown = False
        self.owns_dependents = False
        self.lock = asyncio.Lock()
        self.directory = cluster.profile.base / ("meal-services-" + uuid4().hex)

    def guard(self):
        if self.state != "open": raise ResourceError("Meal-plan services are not open or their outcome is unknown")
        workspace_for(self.root, self.cluster)
        if self.cluster.profile.record() != self.profile: raise ResourceError("Meal-plan episode was rebound")
        receipts, bindings, base = components(self.root)
        if bindings != self.bindings or base != self.recipe_base: raise ResourceError("Meal-plan component receipt changed during the session")
        initialized = json.loads((self.cluster.profile.base / "calendar-initialized.json").read_text(encoding="utf-8"))
        if initialized.get("build_fingerprint") != receipts["calendar"]["build_fingerprint"]:
            raise ResourceError("Meal-plan initialization belongs to another calendar build")

    def record(self):
        return {"schema_version": "meal-plan-business-services-v1", "task_id": TASK_ID,
                "profile": self.profile, "state": self.state, "calls": self.calls, "outcome_unknown": self.outcome_unknown,
                "component_receipt_sha256": self.bindings,
                "sessions": self.sessions, "runtime_task_activated": False, "training_eligible": False,
                "real_model_calls": 0, "training_updates": 0}

    def checkpoint(self):
        """Retain child ownership before yielding control to another request."""
        save(self.directory / "services.json", self.record())
        if self.owns_dependents:
            save(self.cluster.profile.base / "dependent_processes.json", {
                "schema_version": "native-episode-dependents-v1", "profile": self.profile,
                "state": self.state, "expected_children": list(METHODS), "children": self.sessions})

    @asynccontextmanager
    async def open(self):
        if self.state != "new": raise ResourceError("Meal-plan service objects cannot be reopened")
        # Validate ownership and ready state before acquiring locks or spawning children.
        self.state = "open"
        try: self.guard()
        except BaseException:
            self.state = "closed"
            raise
        self.state = "opening"
        self.directory.mkdir()
        try:
            with owned_lock(self.cluster.profile.base / ".meal-services.lock", self.root, "meal_plan_business_services"):
                require_dependents_closed(self.cluster.profile)
                self.owns_dependents = True
                self.checkpoint()  # Incomplete startup also fences reuse after controller death.
                async with AsyncExitStack() as stack:
                    recipe_err = stack.enter_context((self.directory / "recipe.stderr.txt").open("w", encoding="utf-8"))
                    calendar_err = stack.enter_context((self.directory / "calendar.stderr.txt").open("w", encoding="utf-8"))
                    recipe, self.sessions["howtocook"] = await stack.enter_async_context(recipe_session(
                        self.root, self.recipe_base, self.cluster, self.directory, recipe_err))
                    self.checkpoint()
                    calendar, self.sessions["google_calendar"] = await stack.enter_async_context(calendar_session(
                        self.root, self.receipts["calendar"], self.cluster, calendar_err))
                    self.checkpoint()
                    self.clients = {"howtocook": recipe, "google_calendar": calendar}
                    for server, client in self.clients.items():
                        response = await client.list_tools()
                        names = [tool.name for tool in response.tools]
                        if len(set(names)) != len(names) or not set(METHODS[server]) <= set(names):
                            raise ResourceError("Original business MCP schema is missing or duplicated")
                        for tool in response.tools:
                            if tool.name in METHODS[server]: self.schemas.append({"server": server, **tool.model_dump(mode="json")})
                    save(self.directory / "tool_schemas.json", self.schemas)
                    self.state = "open"
                    self.checkpoint()
                    yield self
            self.state = "closed"
        except BaseException:
            self.state = "closed" if set(self.sessions) == set(METHODS) and all(s.get("closed") is True for s in self.sessions.values()) else "unknown"
            raise
        finally:
            self.clients = {}
            self.checkpoint()

    def tools(self):
        self.guard()
        return deepcopy(self.schemas)

    async def call(self, server, name, arguments):
        if not isinstance(server, str) or not isinstance(name, str) or server not in METHODS or name not in METHODS[server]:
            raise ValueError("Tool is outside the fixed meal-plan allowlist")
        if not isinstance(arguments, dict): raise ValueError("Tool arguments must be an object")
        json_bytes(arguments, MAX_ARGUMENT_BYTES)
        async with self.lock:
            self.guard()
            if self.calls >= MAX_CALLS: raise ResourceError("Meal-plan service call budget exhausted")
            self.calls += 1
            try:
                response = await self.clients[server].call_tool(name, deepcopy(arguments))
                result = response.model_dump(mode="json")
                json_bytes(result, MAX_RESPONSE_BYTES)
                return result
            except Exception as exc:
                try:
                    from mcp.shared.exceptions import McpError
                except ImportError:
                    McpError = ()  # CPU contract fixtures do not install the MCP runtime.
                if isinstance(exc, McpError) and exc.error.code == -32602:
                    raise ValueError("Original MCP rejected the tool arguments") from exc
                self.state = "unknown"
                self.outcome_unknown = True
                raise ResourceError("Original MCP call has an unknown outcome; it was not retried") from exc

    async def write_workbook(self, payload):
        async with self.lock:
            self.guard()
            return write_report(self.root, self.cluster, payload)

    async def read_workbook(self):
        async with self.lock:
            self.guard()
            return read_report(self.root, self.cluster)

    async def verify(self):
        async with self.lock:
            self.guard()
            return evaluate(self.root, self.cluster)
