"""Owned original PG email component, persistent request intents, no task reward."""
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import UUID, uuid4

from adapters.email_pg_native import METHODS, SENDER
from integrations.email_build import verify_build
from integrations import email_task_identity as task_identity
from integrations.episode_cluster import require_dependents_closed
from integrations.customer_report_components import snapshot as office_snapshot
from training.email_task_review import TASK_ID, BASE, public_blob
from workenv.core import ResourceError, no_links, save
from workenv.leases import owned_lock
from workenv.process_identity import process_identity

TABLES = ("account_config", "attachments", "drafts", "folders", "messages", "sent_log")
RESET_TABLES = ("attachments", "drafts", "messages", "sent_log")
MAX_REQUESTS = 128


def fingerprint(name, arguments):
    return hashlib.sha256(json.dumps({"name": name, "arguments": arguments}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_arguments(name, arguments):
    if name not in METHODS or type(arguments) is not dict: raise ValueError("Email method is not allowed")
    raw = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()
    if len(raw) > 256 * 1024: raise ValueError("Email argument byte budget")
    if name == "send_email":
        if set(arguments) != {"to", "subject", "body"}: raise ValueError("Email attachments, CC/BCC and HTML are not exposed")
        if (any(not isinstance(arguments[k], str) or not arguments[k] or len(arguments[k]) > n for k, n in (("to", 320), ("subject", 256), ("body", 16384)))
            or not re.fullmatch(r"[^\s,@<>\r\n]+@[^\s,@<>\r\n]+\.[^\s,@<>\r\n]+", arguments["to"])
            or any(c in arguments["subject"] for c in "\r\n")):
            raise ValueError("Email text or single-recipient scope differs")
    elif name == "read_email":
        if set(arguments) != {"email_id"} or not isinstance(arguments["email_id"], str) or not re.fullmatch(r"[1-9][0-9]{0,12}", arguments["email_id"]):
            raise ValueError("Email read identity differs")
    elif (set(arguments) != {"folder", "page", "page_size"} or arguments["folder"] != "Sent"
          or type(arguments["page"]) is not int or not 1 <= arguments["page"] <= 64
          or type(arguments["page_size"]) is not int or not 1 <= arguments["page_size"] <= 50):
        raise ValueError("Email list scope differs")


def snapshot(cluster, *, task_id=TASK_ID):
    identity = task_identity.EmailTaskIdentity(task_id)
    if identity.task_id != TASK_ID:
        from integrations.inventory_data import snapshot as inventory_snapshot
        data = inventory_snapshot(cluster.profile.owner, cluster)
        return {"office": {k: v for k, v in data.items() if k != "email"}, "email": data["email"]}
    office = office_snapshot(cluster)
    result = {"office": office, "email": {}}
    connection = cluster.connect()
    try:
        connection.rollback(); connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with connection.cursor() as cursor:
            for table in TABLES:
                cursor.execute("SELECT count(*) FROM email." + table)
                count = cursor.fetchone()[0]
                if not 0 <= count <= 10000: raise ResourceError("Email snapshot row budget")
                cursor.execute("SELECT row_to_json(t)::text FROM email." + table + " t ORDER BY row_to_json(t)::text LIMIT 10001")
                values = [json.loads(row[0]) for row in cursor.fetchall()]
                if len(values) != count: raise ResourceError("Email snapshot incomplete")
                result["email"][table] = values
    finally: connection.close()
    if len(json.dumps(result).encode()) > 8 * 1024 * 1024: raise ResourceError("Email snapshot byte budget")
    return result


def initialize(root, cluster, *, task_id=TASK_ID):
    identity = task_identity.EmailTaskIdentity(task_id)
    root = Path(root).resolve(); cluster.require_owner(); require_dependents_closed(cluster.profile)
    if cluster.profile.owner != root or not cluster.started: raise ResourceError("Email resource is not owned and active")
    task_identity.require_resolved_requests(root, cluster, identity)
    if task_id != TASK_ID:
        from integrations.inventory_data import initialize as inventory_initialize
        data = inventory_initialize(root, cluster)
        return {"office": {k: v for k, v in data.items() if k != "email"}, "email": data["email"]}
    before = snapshot(cluster)
    run = cluster.profile.base / ("email-preprocess-" + uuid4().hex); run.mkdir()
    workspace = cluster.profile.base / "email-task-workspace"; no_links(workspace, root); workspace.mkdir(exist_ok=True)
    raw, identity = public_blob(root, BASE + "preprocess/main.py")
    script = run / "original_preprocess.py"; script.write_bytes(raw)
    marker = cluster.profile.base / "email-initialized.json"
    save(marker, {"state": "initializing", "task_id": TASK_ID, "profile": cluster.profile.record()})
    result = subprocess.run([sys.executable, str(script), "--agent_workspace", str(workspace)], cwd=run,
        env=cluster.profile.environment(), capture_output=True, timeout=25)
    (run / "stdout.txt").write_bytes(result.stdout); (run / "stderr.txt").write_bytes(result.stderr)
    after = snapshot(cluster)
    if (result.returncode or any(after["email"][n] for n in RESET_TABLES) or any(after["office"][n] for n in ("cells", "sheets", "permissions", "spreadsheets"))
        or before["office"]["readonly_sha256"] != after["office"]["readonly_sha256"]
        or any(before["email"][n] != after["email"][n] for n in ("account_config", "folders"))):
        raise ResourceError("Original email preprocess failed or modified read-only data")
    save(marker, {"state": "ready", "task_id": TASK_ID, "profile": cluster.profile.record(),
        "preprocess_source": identity, "readonly_sha256": after["office"]["readonly_sha256"]})
    return after


def verify_effect(name, arguments, response, before, after, *, task_id=TASK_ID):
    sender = task_identity.EmailTaskIdentity(task_id).sender
    text = "".join(c["text"] for c in response.get("content", []) if c.get("type") == "text")
    if response.get("isError") or text.startswith(("Error", "Email sending failed")): raise ResourceError("Original email outcome unknown")
    if before["office"] != after["office"]: raise ResourceError("Email method modified office source or cloud state")
    left, right = before["email"], after["email"]
    if name == "get_emails":
        if any(left[k] != right[k] for k in TABLES if k != "folders") or not text.startswith(("Folder: Sent\n", "Folder 'Sent' is empty")):
            raise ResourceError("Email list outcome differs")
        # Original select_folder refreshes derived counters, even on a list.
        old = {r["id"]: r for r in left["folders"]}; new = {r["id"]: r for r in right["folders"]}
        if not set(old) <= set(new) or len(set(new) - set(old)) > 1: raise ResourceError("Email list changed folder inventory")
        for fid, row in new.items():
            if row["name"] != "Sent":
                if old.get(fid) != row: raise ResourceError("Email list changed another folder")
                continue
            if fid in old and {k:v for k,v in old[fid].items() if k not in {"message_count", "unread_count"}} != {
                k:v for k,v in row.items() if k not in {"message_count", "unread_count"}}:
                raise ResourceError("Email list changed folder metadata")
            messages = [m for m in right["messages"] if m["folder_id"] == fid]
            if row["message_count"] != len(messages) or row["unread_count"] != sum(m["is_read"] is False for m in messages):
                raise ResourceError("Email list folder counters differ")
        if sum(r["name"] == "Sent" for r in new.values()) != 1: raise ResourceError("Email list Sent folder is missing or duplicated")
        return
    unchanged = ("account_config", "attachments", "drafts")
    if any(left[k] != right[k] for k in unchanged): raise ResourceError("Email method changed unrelated tables")
    old = {r["id"]: r for r in left["messages"]}; new = {r["id"]: r for r in right["messages"]}
    if name == "read_email":
        target = int(arguments["email_id"])
        if set(old) != set(new) or target not in new or left["folders"] != right["folders"] or left["sent_log"] != right["sent_log"]:
            raise ResourceError("Email read changed inventory")
        for mid in old:
            ignore = {"is_read", "updated_at"} if mid == target else set()
            if {k:v for k,v in old[mid].items() if k not in ignore} != {k:v for k,v in new[mid].items() if k not in ignore}:
                raise ResourceError("Email read changed content")
        if new[target]["is_read"] is not True or not text.startswith("Email ID: " + str(target) + "\n"): raise ResourceError("Email read was not confirmed")
        return
    extra = set(new) - set(old)
    if not set(old) <= set(new) or len(extra) != 1 or any(old[k] != new[k] for k in old): raise ResourceError("Email send inventory differs")
    row = new[next(iter(extra))]
    if (row["from_addr"] != sender or row["to_addr"] != [arguments["to"]] or row["subject"] != arguments["subject"]
        or row["body_text"] != arguments["body"] or row.get("cc_addr") or row.get("bcc_addr") or row.get("body_html")
        or not text.startswith("Email sent successfully to " + arguments["to"])):
        raise ResourceError("Email PG send content differs")
    old_logs = {r["id"]: r for r in left["sent_log"]}; new_logs = {r["id"]: r for r in right["sent_log"]}
    added = set(new_logs) - set(old_logs)
    if (len(added) != 1 or not set(old_logs) <= set(new_logs) or any(old_logs[k] != new_logs[k] for k in old_logs)
        or new_logs[next(iter(added))]["message_id"] != row["id"]): raise ResourceError("Email send log is incomplete")
    old_folders = {r["id"]:r["name"] for r in left["folders"]}; new_folders = {r["id"]:r["name"] for r in right["folders"]}
    if (any(new_folders.get(k) != v for k,v in old_folders.items()) or new_folders.get(row["folder_id"]) != "Sent"
        or any(v != "Sent" for k,v in new_folders.items() if k not in old_folders)):
        raise ResourceError("Email send folder scope differs")


class EmailComponents:
    def __init__(self, root, cluster, build_record, *, task_id=TASK_ID):
        self.identity = task_identity.EmailTaskIdentity(task_id)
        self.root, self.cluster, self.build = Path(root).resolve(), cluster, build_record
        if cluster.profile.owner != self.root: raise ResourceError("Foreign email component")
        cluster.require_owner()
        self.run = cluster.profile.base / "email-components-run"
        self.opened, self.entered, self.unknown = False, False, False
        self.client, self.child, self.requests, self.schemas = None, {}, None, []

    def checkpoint(self, state):
        save(self.cluster.profile.base / "dependent_processes.json", {"schema_version": "native-episode-dependents-v1",
            "profile": self.cluster.profile.record(), "state": state, "expected_children": ["email"],
            "children": {"email": self.child} if self.child else {}})

    @asynccontextmanager
    async def open(self):
        if self.entered or self.run.exists(): raise ResourceError("Email component cannot be reopened")
        self.entered = True
        _, python = verify_build(self.root, self.build, check_environment=True)
        initialize(self.root, self.cluster, task_id=self.identity.task_id)
        self.run.mkdir()
        self.requests = {"schema_version": "owned-email-request-journal-v1", "profile": self.cluster.profile.record(),
            "task_id": self.identity.task_id, "sender": self.identity.sender, "entries": {}}
        save(self.run / "requests.json", self.requests)
        self.launch = task_identity.make_launch(self.root, self.cluster, self.build, task_id=self.identity.task_id)
        (self.run / "initialization.json").write_bytes((self.cluster.profile.base / self.identity.marker).read_bytes())
        if hashlib.sha256((self.run / "initialization.json").read_bytes()).hexdigest() != self.launch["task_binding"]["marker_sha256"]:
            raise ResourceError("Email initialization changed before launch")
        save(self.run / "launch.json", self.launch)
        self.checkpoint("opening")
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = self.cluster.profile.environment({"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"})
        parameters = StdioServerParameters(command=str(python), args=["-I", "-B", str(self.root / "adapters/email_pg_native.py"),
            "--resource-id", self.cluster.profile.resource_id, "--slot", str(self.cluster.profile.slot),
            "--task-id", self.identity.task_id], env=env, cwd=str(self.run))
        try:
            with (self.run / "email.stderr.txt").open("w", encoding="utf-8") as err:
                async with stdio_client(parameters, errlog=err) as streams:
                    async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=40)) as client:
                        await client.initialize()
                        observed = json.loads((self.run / "email-process.json").read_text())
                        before = process_identity(observed["pid"])
                        if before["status"] != "alive" or not before.get("creation_id") or observed["profile"] != self.cluster.profile.record():
                            raise ResourceError("Email child identity unknown")
                        self.child = {"profile": self.cluster.profile.record(), "before": before, "closed": False}
                        self.checkpoint("opening")
                        task_identity.validate_process(self.root, self.cluster, self.launch, observed, task_id=self.identity.task_id)
                        self.observed = observed
                        self.client = client
                        self.schemas = [t.model_dump(mode="json") for t in (await client.list_tools()).tools]
                        if {t["name"] for t in self.schemas} != set(METHODS): raise ResourceError("Original email method inventory differs")
                        save(self.run / "tool_schemas.json", self.schemas)
                        self.opened = True; self.checkpoint("open")
                        yield self
        finally:
            self.opened = False
            if self.child:
                after = process_identity(self.child["before"]["pid"])
                self.child["after"] = after
                self.child["closed"] = (after["status"] in ("exited", "not_found") or
                    (after["status"] == "alive" and after.get("creation_id") and after["creation_id"] != self.child["before"]["creation_id"]))
            self.checkpoint("closed" if self.child.get("closed") else "unknown")
            save(self.run / "component_state.json", {"profile": self.cluster.profile.record(), "outcome_unknown": self.unknown,
                "nemo_task_executed": False, "task_passed": None, "reward": None, "external_messages_sent": 0})
            require_dependents_closed(self.cluster.profile)

    def guard(self):
        self.cluster.require_owner()
        if not self.opened or self.unknown: raise ResourceError("Email component is closed, unknown or rebound")
        launch, _ = task_identity._read(self.root, self.run / "launch.json", limit=2 * 1024 * 1024)
        if launch != self.launch: raise ResourceError("Email launch record changed")
        task_identity.validate_process(self.root, self.cluster, launch, self.observed, task_id=self.identity.task_id)
        journal = self.run / "requests.json"
        if journal.stat().st_size > 32 * 1024 * 1024 or json.loads(journal.read_text()) != self.requests:
            raise ResourceError("Email request journal changed or exceeds budget")
        if any(e.get("state") != "committed" for e in self.requests["entries"].values()):
            raise ResourceError("Email request intent remains unresolved; replay refused")

    def save_request_snapshot(self, path, data):
        """Standalone email evidence keeps its original full-snapshot format."""
        save(path, data)

    def take_snapshot(self):
        return snapshot(self.cluster, task_id=self.identity.task_id)

    async def call(self, name, arguments, request_id):
        arguments = deepcopy(arguments)
        validate_arguments(name, arguments)
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id: raise ValueError("Canonical email request UUID required")
        self.guard()
        with owned_lock(self.run / ".request.lock", self.root, "email_request"):
            self.guard()
            signature = fingerprint(name, arguments)
            previous = self.requests["entries"].get(request_id)
            if previous:
                if previous["fingerprint"] != signature: raise ValueError("Email request identity reused with different parameters")
                if previous["state"] != "committed": raise ResourceError("Email request outcome unknown; replay refused")
                return deepcopy(previous["response"])
            if len(self.requests["entries"]) >= MAX_REQUESTS: raise ResourceError("Email request history budget")
            before = self.take_snapshot()
            before_path = self.run / (request_id + ".before.json"); self.save_request_snapshot(before_path, before)
            record = {"name": name, "arguments": arguments, "fingerprint": signature, "state": "prepared",
                      "before_ref": before_path.relative_to(self.root).as_posix()}
            self.requests["entries"][request_id] = record; save(self.run / "requests.json", self.requests)
            try:
                response = (await self.client.call_tool(name, arguments)).model_dump(mode="json")
                if len(json.dumps(response).encode()) > 256 * 1024: raise ResourceError("Email original response exceeds budget")
                record["response"] = response; save(self.run / "requests.json", self.requests)
                after = self.take_snapshot()
                after_path = self.run / (request_id + ".after.json"); self.save_request_snapshot(after_path, after)
                record.update(response=response, after_ref=after_path.relative_to(self.root).as_posix())
                verify_effect(name, arguments, response, before, after, task_id=self.identity.task_id)
                record["state"] = "committed"; save(self.run / "requests.json", self.requests)
                return deepcopy(response)
            except BaseException as exc:
                self.unknown = True; record.update(state="unknown", error_type=type(exc).__name__)
                save(self.run / "requests.json", self.requests)
                raise ResourceError("Email call outcome unknown; no operation retried") from exc
