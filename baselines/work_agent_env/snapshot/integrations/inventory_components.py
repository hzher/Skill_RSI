"""Owned original inventory tools; component measurements, never task rewards."""
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

from adapters import inventory_alert_evaluation as evaluation
from integrations import inventory_data as data
from integrations.calendar_component import calendar_session, require_schema, verify_build
from integrations.email_components import EmailComponents, fingerprint, validate_arguments as email_arguments
from integrations.email_task_identity import _read
from integrations.episode_cluster import require_dependents_closed
from integrations.order_analysis_resource import woocommerce_session
from workenv.core import ResourceError, contained, save
from workenv.leases import owned_lock
from workenv.process_identity import process_identity

METHODS = {"woocommerce": ("woo_products_list",), "google_calendar": ("create_event", "get_event", "list_events"),
           "email": ("send_email", "get_emails", "read_email")}
JOURNAL_SCHEMA = "inventory-original-request-journal-v1"
IMMUTABLE = ("products", "readonly_tables", "readonly_sha256", "sql_zero_stock")


def validate_call(server, name, arguments):
    if server not in METHODS or name not in METHODS[server] or type(arguments) is not dict:
        raise ValueError("Inventory original method is not allowed")
    if len(json.dumps(arguments, allow_nan=False).encode()) > 256 * 1024: raise ValueError("Inventory argument byte budget")
    if server == "email": return email_arguments(name, arguments)
    if server == "woocommerce":
        if (set(arguments) != {"page", "perPage"} or type(arguments["page"]) is not int
            or not 1 <= arguments["page"] <= 64 or type(arguments["perPage"]) is not int or arguments["perPage"] != 10):
            raise ValueError("Inventory product pagination scope differs")
        return
    keys = {"create_event": {"summary", "description", "start", "end"}, "get_event": {"eventId"},
            "list_events": {"timeMin", "timeMax", "maxResults"}}[name]
    if set(arguments) != keys: raise ValueError("Inventory calendar argument fields differ")
    if name == "get_event":
        if not isinstance(arguments["eventId"], str) or str(UUID(arguments["eventId"])) != arguments["eventId"]:
            raise ValueError("Canonical calendar event UUID required")
    elif name == "list_events":
        if type(arguments["maxResults"]) is not int or not 1 <= arguments["maxResults"] <= 100:
            raise ValueError("Inventory calendar list budget")
        for key in ("timeMin", "timeMax"): data.validation.instant(arguments[key])
        if data.validation.instant(arguments["timeMin"]) >= data.validation.instant(arguments["timeMax"]):
            raise ValueError("Calendar list interval is empty")
    else:
        data.validation.line_text(arguments["summary"], ValueError, limit=256)
        data.validation.line_text(arguments["description"], ValueError, blank=True, limit=16384)
        for key in ("start", "end"):
            value = arguments[key]
            if type(value) is not dict or set(value) != {"dateTime", "timeZone"}: raise ValueError("Calendar instant shape differs")
            data.validation.instant(value["dateTime"])
            data.validation.line_text(value["timeZone"], ValueError, limit=128)
        if data.validation.instant(arguments["start"]["dateTime"]) >= data.validation.instant(arguments["end"]["dateTime"]):
            raise ValueError("Calendar event interval is empty")


def save_snapshot(root, path, state, source_path):
    source, sha = _read(root, source_path, limit=data.MAX_SNAPSHOT_BYTES)
    if any(state[k] != source[k] for k in IMMUTABLE): raise ResourceError("Inventory read-only SQL source changed")
    save(path, {"schema_version": "inventory-state-snapshot-v1", "source_ref": source_path.relative_to(root).as_posix(),
        "source_sha256": sha, "state": {k: v for k, v in state.items() if k not in IMMUTABLE}})


def load_snapshot(root, path, source_path):
    record, _ = _read(root, path, limit=data.MAX_SNAPSHOT_BYTES)
    source, sha = _read(root, source_path, limit=data.MAX_SNAPSHOT_BYTES)
    if (record.get("schema_version") != "inventory-state-snapshot-v1" or record.get("source_sha256") != sha
        or record.get("source_ref") != source_path.relative_to(root).as_posix()
        or type(record.get("state")) is not dict or set(record["state"]) != {"events", "email"}):
        raise ResourceError("Inventory compact snapshot source differs")
    state = {**deepcopy(record["state"]), **{k: source[k] for k in IMMUTABLE}}
    data.validate_snapshot(state)
    return state


def verify_effect(server, name, arguments, response, before, after):
    if any(before[k] != after[k] for k in IMMUTABLE): raise ResourceError("Inventory tool changed read-only source")
    text = "".join(b["text"] for b in response.get("content", []) if b.get("type") == "text")
    if response.get("isError") or text.startswith(("Error", "Failed", "Email sending failed")):
        raise ResourceError("Original inventory operation returned an error")
    if server == "email":
        from integrations.email_components import verify_effect as mail_effect
        mail_effect(name, arguments, response,
            {"office": {k: v for k, v in before.items() if k != "email"}, "email": before["email"]},
            {"office": {k: v for k, v in after.items() if k != "email"}, "email": after["email"]}, task_id=data.TASK_ID)
    elif server == "woocommerce" or name != "create_event":
        if before != after: raise ResourceError("Original read modified inventory state")
        if server == "woocommerce": data.decode_products(response)
    else:
        if before["email"] != after["email"]: raise ResourceError("Calendar operation changed mail")
        old = {r["id"]: r for r in before["events"]}; new = {r["id"]: r for r in after["events"]}
        if len(old) != len(before["events"]) or len(new) != len(after["events"]): raise ResourceError("Duplicate Calendar event identity")
        extra = set(new) - set(old)
        if not set(old) <= set(new) or len(extra) != 1 or any(old[k] != new[k] for k in old):
            raise ResourceError("Calendar create inventory differs")
        event = new[next(iter(extra))]
        if any(event[k] != arguments[k] for k in ("summary", "description")):
            raise ResourceError("Calendar create text differs")
        for side in ("start", "end"):
            if (data.validation.instant(event[side + "_datetime"]) != data.validation.instant(arguments[side]["dateTime"])
                or event[side + "_timezone"] != arguments[side]["timeZone"]): raise ResourceError("Calendar create instant differs")


class _InventoryMail(EmailComponents):
    def __init__(self, owner, build):
        self.owner = owner
        super().__init__(owner.root, owner.cluster, build, task_id=data.TASK_ID)

    def checkpoint(self, state):
        if self.child: self.owner.children["email"] = self.child
        self.owner.checkpoint(state)


class InventoryComponents:
    def __init__(self, root, cluster, build):
        self.root, self.cluster = Path(root).resolve(), cluster
        data.require_cluster(self.root, cluster)
        self.run = cluster.profile.base / "inventory-components-run"
        self.children, self.clients, self.schemas = {}, {}, []
        self.opened, self.entered, self.unknown = False, False, False
        self.mail = _InventoryMail(self, build)
        self.requests = {"schema_version": JOURNAL_SCHEMA, "task_id": data.TASK_ID, "profile": cluster.profile.record(), "entries": {}}
        self.calendar_path = self.root / "evidence/mainline/calendar_build.json"
        self.calendar, self.calendar_sha = _read(self.root, self.calendar_path, limit=256 * 1024)
        verify_build(self.root, self.calendar)

    def checkpoint(self, state):
        if state == "closed" and (set(self.children) != set(METHODS) or not all(c.get("closed") for c in self.children.values())):
            state = "unknown"
        save(self.cluster.profile.base / "dependent_processes.json", {"schema_version": "native-episode-dependents-v1",
            "profile": self.cluster.profile.record(), "state": state, "expected_children": list(METHODS), "children": self.children})

    @asynccontextmanager
    async def open(self):
        if self.entered or self.run.exists(): raise ResourceError("Inventory component cannot be reopened")
        self.entered = True; self.run.mkdir(); save(self.run / "requests.json", self.requests)
        try:
            async with self.mail.open():
                self.initial = data.snapshot(self.root, self.cluster)
                save(self.run / "initial_snapshot.json", self.initial)
                if require_schema(self.cluster) != 0: raise ResourceError("Inventory Calendar was not initialized empty")
                self.calendar_marker = {"state": "ready", "profile": self.cluster.profile.record(), "task_id": data.TASK_ID,
                    "build_fingerprint": self.calendar["build_fingerprint"],
                    "inventory_marker_sha256": self.mail.launch["task_binding"]["marker_sha256"],
                    "initialization_scope": "inventory_original_preprocess_not_meal_preprocess"}
                save(self.cluster.profile.base / "calendar-initialized.json", self.calendar_marker)
                async with AsyncExitStack() as stack:
                    wc, self.children["woocommerce"] = await stack.enter_async_context(woocommerce_session(self.root, self.cluster, self.run))
                    self.checkpoint("opening")
                    err = stack.enter_context((self.run / "calendar.stderr.txt").open("w", encoding="utf-8"))
                    cal, self.children["google_calendar"] = await stack.enter_async_context(calendar_session(self.root, self.calendar, self.cluster, err))
                    self.clients = {"woocommerce": wc, "google_calendar": cal}
                    for server, client in self.clients.items():
                        tools = (await client.list_tools()).tools
                        if not set(METHODS[server]) <= {t.name for t in tools}: raise ResourceError("Inventory original tool schema missing")
                        self.schemas.extend({"server": server, **t.model_dump(mode="json")} for t in tools if t.name in METHODS[server])
                    self.schemas.extend({"server": "email", **t} for t in self.mail.schemas)
                    save(self.run / "tool_schemas.json", self.schemas)
                    self.opened = True; self.checkpoint("open")
                    yield self
        finally:
            self.opened = False
            save(self.run / "component_state.json", {"profile": self.cluster.profile.record(), "outcome_unknown": self.unknown or self.mail.unknown,
                "runtime_task_activated": False, "task_passed": None, "reward": None, "external_messages_sent": 0})
            require_dependents_closed(self.cluster.profile)

    def guard(self):
        if not self.opened or self.unknown: raise ResourceError("Inventory component is closed or unknown")
        self.mail.guard()
        record, _ = _read(self.root, self.run / "requests.json", limit=32 * 1024 * 1024)
        if record != self.requests or any(e.get("state") != "committed" for e in record["entries"].values()):
            raise ResourceError("Inventory request intent unresolved or changed")
        marker, _ = _read(self.root, self.cluster.profile.base / "calendar-initialized.json")
        _, sha = _read(self.root, self.calendar_path, limit=256 * 1024)
        if marker != self.calendar_marker or sha != self.calendar_sha: raise ResourceError("Inventory Calendar binding changed")
        verify_build(self.root, self.calendar)
        for child in self.children.values():
            actual = process_identity(child["before"]["pid"])
            if actual.get("status") != "alive" or actual.get("creation_id") != child["before"]["creation_id"]:
                raise ResourceError("Inventory child exited, changed or is unknown")
        current = data.snapshot(self.root, self.cluster)
        if any(current[k] != self.initial[k] for k in IMMUTABLE): raise ResourceError("Inventory source changed")
        data.artifacts.require_no_pending(self.root, self.cluster, self.cluster.profile.base / "inventory-workspace" / data.validation.FILENAME)

    async def call(self, server, name, arguments, request_id):
        arguments = deepcopy(arguments); validate_call(server, name, arguments)
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id: raise ValueError("Canonical inventory request UUID required")
        self.guard()
        with owned_lock(self.run / ".request.lock", self.root, "inventory_request"):
            self.guard()
            signature = fingerprint(server + "/" + name, arguments)
            previous = self.requests["entries"].get(request_id)
            if previous:
                if previous["fingerprint"] != signature: raise ValueError("Inventory request ID conflict")
                return deepcopy(previous["response"])
            if len(self.requests["entries"]) >= 128: raise ResourceError("Inventory request budget")
            before = data.snapshot(self.root, self.cluster)
            if name == "get_event" and arguments["eventId"] not in {r["id"] for r in before["events"]}:
                raise ResourceError("Calendar event is not owned by this inventory resource")
            source = self.run / "initial_snapshot.json"
            before_path = self.run / (request_id + ".before.json"); save_snapshot(self.root, before_path, before, source)
            entry = {"server": server, "name": name, "arguments": arguments, "fingerprint": signature, "state": "prepared",
                "before_ref": before_path.relative_to(self.root).as_posix()}
            self.requests["entries"][request_id] = entry; save(self.run / "requests.json", self.requests)
            try:
                response = await self.mail.call(name, arguments, request_id) if server == "email" else (
                    await self.clients[server].call_tool(name, arguments)).model_dump(mode="json")
                if len(json.dumps(response, allow_nan=False).encode()) > 256 * 1024: raise ResourceError("Inventory response budget")
                entry["response"] = response; save(self.run / "requests.json", self.requests)
                after = data.snapshot(self.root, self.cluster)
                after_path = self.run / (request_id + ".after.json"); save_snapshot(self.root, after_path, after, source)
                entry["after_ref"] = after_path.relative_to(self.root).as_posix()
                verify_effect(server, name, arguments, response, before, after)
                entry["state"] = "committed"; save(self.run / "requests.json", self.requests)
                return deepcopy(response)
            except BaseException as exc:
                self.unknown = True; entry.update(state="unknown", error_type=type(exc).__name__)
                if "after_ref" not in entry:
                    try:
                        after_path = self.run / (request_id + ".after.json")
                        save_snapshot(self.root, after_path, data.snapshot(self.root, self.cluster), source)
                        entry["after_ref"] = after_path.relative_to(self.root).as_posix()
                    except Exception as observation_error:
                        entry["observation_error_type"] = type(observation_error).__name__
                save(self.run / "requests.json", self.requests)
                raise ResourceError("Inventory operation unknown; no request replayed") from exc

    def write_report(self, text):
        self.guard()
        return data.artifacts.write_report(self.root, self.cluster, text)

    def evaluate(self):
        self.guard()
        return evaluation.evaluate(self.root, self.cluster)

    async def collect_sources(self):
        self.guard()
        async def call(name, args): return await self.call("woocommerce", name, args, str(uuid4()))
        products, pages = await data.collect_products(call, self.initial["products"])
        path = self.run / "original-pages.json"; save(path, pages)
        save(self.cluster.profile.base / evaluation.SOURCE_NAME, {"schema_version": "inventory-evaluation-source-v1",
            "task_id": data.TASK_ID, "profile": self.cluster.profile.record(), "contract_source": data.validation.source_identity(),
            "readonly_sha256": self.initial["readonly_sha256"], "pages_ref": path.relative_to(self.root).as_posix(),
            "pages_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        return data.validation.expected(products, source_binding=data.validation.source_identity())
