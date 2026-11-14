"""Compose existing owned Woo/Sheets/PG-email sessions without task activation."""
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID

from adapters import vip_email_validation as validation
from adapters.customer_spending_validation import integer, number
from integrations.customer_report_components import sheets_session
from integrations.order_analysis_resource import woocommerce_session
from integrations.email_components import EmailComponents, snapshot, fingerprint, validate_arguments as email_arguments
from integrations.episode_cluster import require_dependents_closed
from workenv.core import ResourceError, save, contained
from workenv.leases import owned_lock

METHODS = {"woocommerce": ("woo_customers_list",), "google_sheet": ("create_spreadsheet", "list_sheets", "rename_sheet", "create_sheet", "update_cells", "get_sheet_data"),
           "email": ("send_email", "get_emails", "read_email")}


def jsonable(value): return json.loads(json.dumps(value, default=str, allow_nan=False))


def save_snapshot(root, path, data, source_path):
    source = json.loads(source_path.read_text())
    keys = ('customers','orders','readonly_sha256')
    if any(source['office'][key]!=data['office'][key] for key in keys):raise ResourceError('VIP immutable SQL source differs')
    state=deepcopy(data)
    for key in ('customers','orders'):state['office'].pop(key)
    save(path,{'schema_version':'vip-state-snapshot-v1','state':state,
        'readonly_source_ref':source_path.relative_to(root).as_posix(),'readonly_source_sha256':hashlib.sha256(source_path.read_bytes()).hexdigest()})


def load_snapshot(root,path,source_path):
    from integrations.mainline_audit import read_json
    record=read_json(path,root,limit=8*1024*1024)
    if record.get('schema_version')!='vip-state-snapshot-v1' or record['readonly_source_ref']!=source_path.relative_to(root).as_posix():
        raise ValueError('VIP immutable SQL source rebound')
    source=read_json(contained(root,record['readonly_source_ref']),root,limit=8*1024*1024)
    if hashlib.sha256(source_path.read_bytes()).hexdigest()!=record['readonly_source_sha256']:raise ValueError('VIP immutable SQL source bytes changed')
    state=record['state']
    if state['office']['readonly_sha256']!=source['office']['readonly_sha256'] or any(k in state['office'] for k in ('customers','orders')):
        raise ValueError('VIP compact source identity differs')
    for key in ('customers','orders'):state['office'][key]=source['office'][key]
    return state


def decode(reply):
    if reply.get("isError"): raise ResourceError("Original VIP tool returned an error")
    try: value = json.loads("".join(b["text"] for b in reply["content"] if b["type"] == "text"))
    except (KeyError, ValueError, TypeError) as exc: raise ResourceError("Original VIP JSON response differs") from exc
    if isinstance(value, dict) and "error" in value: raise ResourceError("Original VIP tool reported an error")
    return value


def validate_call(server, name, args):
    if server not in METHODS or name not in METHODS[server] or type(args) is not dict: raise ValueError("VIP original method is not allowed")
    if len(json.dumps(args, allow_nan=False).encode()) > 256 * 1024: raise ValueError("VIP argument byte budget")
    if server == "email": return email_arguments(name, args)
    if server == "woocommerce":
        if set(args) != {"page", "perPage"} or type(args["page"]) is not int or not 1 <= args["page"] <= 64 or type(args["perPage"]) is not int or args["perPage"] != 10:
            raise ValueError("VIP customer pagination scope differs")
        return
    keys = {"create_spreadsheet": {"title"}, "list_sheets": {"spreadsheet_id"}, "rename_sheet": {"spreadsheet", "sheet", "new_name"},
            "create_sheet": {"spreadsheet_id", "title"}, "update_cells": {"spreadsheet_id", "sheet", "range", "data"},
            "get_sheet_data": {"spreadsheet_id", "sheet", "range"}}[name]
    if set(args) != keys: raise ValueError("VIP Sheets argument scope differs")
    if name == "create_spreadsheet" and args["title"] != validation.TITLE: raise ValueError("VIP fixed spreadsheet title required")
    if name == "create_sheet" and args["title"] != "Store Summary": raise ValueError("VIP fixed second sheet required")
    if name == "rename_sheet" and args["new_name"] != "Top Customers": raise ValueError("VIP fixed first sheet required")
    for key in keys - {"data"}:
        if not isinstance(args[key], str) or not args[key] or len(args[key]) > 256: raise ValueError("VIP Sheets text identity budget")
    if "range" in keys:
        if args["sheet"] not in validation.HEADERS: raise ValueError("VIP fixed sheet required")
        matched = re.fullmatch(r"([A-E])([1-9]|10|11):([A-E])([1-9]|10|11)", args["range"])
        if not matched: raise ValueError("VIP bounded cell range required")
        c1,r1,c2,r2 = matched.groups(); r1,r2 = int(r1),int(r2)
        if c1 > c2 or r1 > r2 or args["sheet"] == "Store Summary" and (c2 > "B" or r2 > 5): raise ValueError("VIP cell range differs")
        if name == "update_cells":
            grid = args["data"]
            if not isinstance(grid, list) or len(grid) != r2-r1+1 or any(not isinstance(row, list) or len(row) != ord(c2)-ord(c1)+1 for row in grid):
                raise ValueError("VIP grid does not fill its bounded range")
            for row in grid:
                for value in row:
                    if not isinstance(value, (str, int, float)) or isinstance(value, str) and (len(value) > 4096 or value.startswith("=")):
                        raise ValueError("VIP cell literal budget")


def bind_sheet(args, before):
    office = before["office"]
    if "spreadsheet_id" not in args and "spreadsheet" not in args:
        if office["spreadsheets"]: raise ValueError("VIP spreadsheet already exists; reuse original request ID")
        return
    target = args.get("spreadsheet_id", args.get("spreadsheet"))
    if len(office["spreadsheets"]) != 1 or office["spreadsheets"][0]["id"] != target: raise ResourceError("VIP cloud identity is not owned")
    if "sheet" in args and args["sheet"] not in {s["title"] for s in office["sheets"] if s["spreadsheet_id"] == target}:
        raise ResourceError("VIP tab identity is not owned")


class _CombinedEmail(EmailComponents):
    def __init__(self, owner, build):
        self.owner = owner
        super().__init__(owner.root, owner.cluster, build)

    def checkpoint(self, state):
        if self.child: self.owner.children["email"] = self.child
        self.owner.checkpoint(state)

    def save_request_snapshot(self, path, data):
        save_snapshot(self.owner.root, path, data, self.owner.run / "initial_snapshot.json")


class VipEmailComponents:
    def __init__(self, root, cluster, build):
        self.root, self.cluster = Path(root).resolve(), cluster
        if self.root != cluster.profile.owner: raise ResourceError("Foreign VIP component")
        cluster.require_owner()
        self.run = cluster.profile.base / "vip-components-run"
        self.children, self.clients, self.schemas = {}, {}, []
        self.opened, self.entered, self.unknown = False, False, False
        self.mail = _CombinedEmail(self, build)
        self.requests = {"schema_version": "vip-original-request-journal-v1", "profile": cluster.profile.record(), "entries": {}}

    def checkpoint(self, state):
        if state == "closed" and (set(self.children) != set(METHODS) or not all(c.get("closed") for c in self.children.values())): state = "unknown"
        save(self.cluster.profile.base / "dependent_processes.json", {"schema_version": "native-episode-dependents-v1",
            "profile": self.cluster.profile.record(), "state": state, "expected_children": list(METHODS), "children": self.children})

    @asynccontextmanager
    async def open(self):
        if self.entered or self.run.exists(): raise ResourceError("VIP component cannot be reopened")
        self.entered = True; self.run.mkdir(); save(self.run / "requests.json", self.requests)
        try:
            async with self.mail.open():
                self.initial = snapshot(self.cluster); save(self.run / "initial_snapshot.json", self.initial)
                async with AsyncExitStack() as stack:
                    wc, self.children["woocommerce"] = await stack.enter_async_context(woocommerce_session(self.root, self.cluster, self.run))
                    self.checkpoint("opening")
                    sheets, self.children["google_sheet"] = await stack.enter_async_context(sheets_session(self.root, self.cluster, self.run))
                    self.clients = {"woocommerce": wc, "google_sheet": sheets}
                    for server, client in self.clients.items():
                        tools = (await client.list_tools()).tools
                        if not set(METHODS[server]) <= {t.name for t in tools}: raise ResourceError("VIP original schema missing")
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
        if not self.opened or self.unknown: raise ResourceError("VIP component closed or unknown")
        self.mail.guard()
        if self.requests != json.loads((self.run / "requests.json").read_text()) or any(e["state"] != "committed" for e in self.requests["entries"].values()):
            raise ResourceError("VIP request intent unresolved or changed")
        if snapshot(self.cluster)["office"]["readonly_sha256"] != self.initial["office"]["readonly_sha256"]:
            raise ResourceError("VIP customer source changed")

    async def call(self, server, name, args, request_id):
        args = deepcopy(args); validate_call(server, name, args)
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id: raise ValueError("Canonical VIP request UUID required")
        self.guard()
        with owned_lock(self.run / ".request.lock", self.root, "vip_request"):
            signature = fingerprint(server + "/" + name, args); previous = self.requests["entries"].get(request_id)
            if previous:
                if previous["fingerprint"] != signature: raise ValueError("VIP request ID conflict")
                return deepcopy(previous["response"])
            if len(self.requests["entries"]) >= 128: raise ResourceError("VIP request history budget")
            before = snapshot(self.cluster)
            if server == "google_sheet": bind_sheet(args, before)
            before_path = self.run / (request_id + ".before.json"); save_snapshot(self.root,before_path,before,self.run/'initial_snapshot.json')
            entry = {"server": server, "name": name, "arguments": args, "fingerprint": signature, "state": "prepared",
                "before_ref": before_path.relative_to(self.root).as_posix()}
            self.requests["entries"][request_id] = entry; save(self.run / "requests.json", self.requests)
            try:
                result = (await self.mail.call(name, args, request_id)) if server == "email" else (await self.clients[server].call_tool(name, args)).model_dump(mode="json")
                if len(json.dumps(result).encode()) > 256 * 1024: raise ResourceError("VIP original response too large")
                entry["response"] = result; save(self.run / "requests.json", self.requests)
                after = snapshot(self.cluster); after_path = self.run / (request_id + ".after.json"); save_snapshot(self.root,after_path,after,self.run/'initial_snapshot.json')
                entry["after_ref"] = after_path.relative_to(self.root).as_posix()
                if result.get("isError"): raise ResourceError("VIP original operation reported an error")
                if before["office"]["readonly_sha256"] != after["office"]["readonly_sha256"]: raise ResourceError("VIP source mutation")
                if server == "woocommerce" and before != after: raise ResourceError("VIP read modified data")
                if server == "google_sheet":
                    if before["email"] != after["email"]: raise ResourceError("VIP cloud operation changed mail")
                    if name in ("list_sheets", "get_sheet_data") and before != after: raise ResourceError("VIP cloud read modified data")
                    if name != "list_sheets": decode(result)
                entry["state"] = "committed"; save(self.run / "requests.json", self.requests)
                return deepcopy(result)
            except BaseException as exc:
                self.unknown = True; entry.update(state="unknown", error_type=type(exc).__name__)
                save(self.run / "requests.json", self.requests)
                raise ResourceError("VIP operation unknown; no request replayed") from exc


def check_customers(original, data):
    validation.source_rows(original, ("id", "first_name", "last_name", "email", "orders_count", "total_spent"))
    truth = {c["id"]: c for c in data["office"]["customers"]}
    if {c["id"] for c in original} != set(truth): raise ResourceError("VIP original customer inventory incomplete")
    for row in original:
        for field in ("first_name", "last_name", "email", "orders_count", "total_spent"):
            left, right = row[field], truth[row["id"]][field]
            if field in ("orders_count", "total_spent"): left,right = number(left),number(right)
            if left != right: raise ResourceError("VIP original customer field differs")


async def collect_sources(controller, request_id_factory):
    original, pages = [], []
    for page in range(1, 65):
        rows = decode(await controller.call("woocommerce", "woo_customers_list", {"page": page, "perPage": 10}, request_id_factory()))
        if not isinstance(rows, list): raise ResourceError("VIP original customer page is not an array")
        original.extend(rows); pages.append({"page": page, "rows": len(rows)})
        if len(rows) < 10: break
    else: raise ResourceError("VIP pagination did not terminate")
    data = snapshot(controller.cluster); check_customers(original, data)
    truth = validation.expected(original, source_binding=validation.source_identity())
    save(controller.run / "original_customers.json", {"customers": original, "pages": pages, "readonly_sha256": data["office"]["readonly_sha256"]})
    return truth


def sql_truth(cluster, data):
    connection = cluster.connect()
    try:
        connection.rollback(); connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with connection.cursor() as cur:
            cur.execute("""SELECT btrim(btrim(first_name)||' '||btrim(last_name)), lower(btrim(email)), orders_count,
                round(total_spent::numeric,2), CASE WHEN orders_count>0 THEN round(total_spent::numeric/orders_count,2) ELSE 0 END
                FROM wc.customers ORDER BY total_spent::numeric DESC LIMIT 10""")
            rows = [dict(zip(validation.HEADERS["Top Customers"], row)) for row in cur.fetchall()]
            cur.execute("""WITH all_c AS (SELECT count(*) n,round(sum(total_spent::numeric),2) revenue FROM wc.customers),
                vip AS (SELECT round(sum(total_spent::numeric),2) revenue FROM
                    (SELECT total_spent FROM wc.customers ORDER BY total_spent::numeric DESC LIMIT 10) t)
                SELECT n,all_c.revenue,CASE WHEN all_c.revenue>0 THEN round(vip.revenue/all_c.revenue*100,1) ELSE 0 END,
                    CASE WHEN n>0 THEN round(all_c.revenue/n,2) ELSE 0 END FROM all_c,vip""")
            metrics = dict(zip(("Total_Customers", "Total_Revenue", "VIP_Revenue_Share_Pct", "Avg_Customer_Spend"),cur.fetchone()))
    finally: connection.close()
    expected = validation.expected(data["office"]["customers"], source_binding=validation.source_identity())
    if ({r["Email"]:r for r in rows} != {r["Email"]:r for r in expected["rows"]} or metrics != expected["metrics"]
        or snapshot(cluster) != data): raise ResourceError("VIP independent SQL/Python inputs or aggregates differ")
    return jsonable({"rows": rows, "metrics": metrics})
