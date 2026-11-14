"""Bounded resource transport to original Toolathlon MCP and evaluator on Windows.

This process is a resource backend, not an agent or environment loop. The public
environment API is the NeMo Gym Resources Server in integrations/nemo_resources.py.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import threading
import uuid

from adapters import toolathlon_native as native
from integrations.resource_ledger import ResourceLedger
from integrations.workbook_tool import PARAMETERS, render_workbook
from integrations.synthetic_backend import SyntheticResources, SyntheticConflict
from integrations.runtime_identity import BRIDGE_FILES
from integrations.isolated_gateway import IsolatedGateway, IsolatedConflict
from integrations.task_routing import TaskRouter
from integrations.resource_transport import MAX_REQUEST_FRAME_BYTES, wire_json

ALLOWED = {
    "woocommerce": {"woo_orders_list"},
    "google_sheet": {"create_spreadsheet", "list_sheets", "rename_sheet", "update_cells", "get_sheet_data"},
}
MAX_BODY = MAX_REQUEST_FRAME_BYTES
MAX_SESSIONS = 128
LOADED_SOURCE_SHA256 = {name: hashlib.sha256((native.ROOT / name).read_bytes()).hexdigest() for name in BRIDGE_FILES}


class Conflict(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def session_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("A canonical UUID resources_session_id is required")
    return value


class Resources:
    def __init__(self, ledger=None, *, resource_backend="shared", adapter=None):
        if adapter is not None and resource_backend != "isolated":
            raise ValueError("Task-specific business adapters require isolated resources")
        self.adapter = adapter
        self.task_router = TaskRouter(native.ROOT, resource_backend)
        self.ledger = ledger or ResourceLedger(native.ROOT / ".runtime/nemo-vm/resource-sessions.json", native.ROOT)
        self.sessions = {sid: {"binding": row["binding"], "closed": True}
                         for sid, row in self.ledger.closed_rows().items()}
        self.active = None
        self.lock = asyncio.Lock()

    def get(self, body):
        sid = session_id(body.get("resources_session_id"))
        state = self.sessions.get(sid)
        if state is None or state["closed"] or not state.get("ready") or self.ledger.rows[sid]["state"] != "active":
            raise Conflict("Resource session is missing or closed")
        if body.get("episode_id") != state["binding"]["episode_id"]:
            raise Conflict("Episode identity does not match the resource session")
        return sid, state

    async def dispatch(self, route, body):
        async with self.lock:  # One original task owns the shared, dedicated PG seed.
            await self._reap()
            return await self._dispatch(route, body)

    async def recover(self):
        # Called only after acquiring the dedicated task lock and verifying PG ownership.
        async with self.lock:
            for sid, row in self.ledger.rows.items():
                if row["state"] == "closed": continue
                if row["run_ref"] is not None:
                    run = native.ROOT / row["run_ref"]
                    if run.parent != native.ROOT / "runs" or native.run_directory(run.name) != run.resolve():
                        raise RuntimeError("Persisted run path is outside owned task runs")
                    native.reset(run, archive=True)
                self.ledger.update(sid, state="closed")
                self.sessions[sid] = {"binding": row["binding"], "closed": True}

    async def _reap(self):
        for sid in self.ledger.expired():
            state = self.sessions[sid]
            await self._dispatch("/close_session", {"resources_session_id": sid, "episode_id": state["binding"]["episode_id"]})

    async def reaper(self):
        while True:
            await asyncio.sleep(15)
            async with self.lock:
                try:
                    await self._reap()
                except Exception as exc:
                    # Retain open/closing state after cleanup failure; never report release.
                    print(json.dumps({"resource_reaper_error": type(exc).__name__}), flush=True)

    async def _dispatch(self, route, body):
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        if route == "/seed_session":
            sid = session_id(body.get("resources_session_id"))
            if set(body) != {"resources_session_id", "episode_id", "task_id", "task_data"}:
                raise ValueError("Unexpected seed fields")
            task = body["task_id"]
            if not isinstance(task, dict) or task.get("task_id") != (self.adapter.task_id if self.adapter else native.TASK_ID) or not isinstance(task.get("taskset"), str) or not task["taskset"].startswith("toolathlon_gym:") or body["task_data"]:
                raise ValueError("Only the pinned original Toolathlon seed is accepted")
            if sid in self.sessions:
                state = self.sessions[sid]
                if state["closed"] or not state.get("ready") or self.ledger.rows[sid]["state"] != "active" or state["binding"] != body:
                    raise Conflict("Session was closed, failed, or has a different binding")
                return {"resources_session_id": sid}
            if self.active is not None or len(self.ledger.rows) >= MAX_SESSIONS:
                raise Conflict("Dedicated task resource is busy or session budget exhausted")
            task_route = self.task_router.authorize(body)
            self.ledger.seed(sid, body)
            state = {"binding": body, "task_route": task_route, "closed": False, "ready": False, "trace": [], "requests": {}}
            self.sessions[sid] = state  # Failed resets cannot silently recreate a session.
            self.active = sid
            run = self.adapter.new_run() if self.adapter else native.new_run("nemo_resources")
            state["run"] = run
            self.ledger.update(sid, run_ref=run.relative_to(native.ROOT).as_posix())
            native.save(run / "nemo_binding.json", body)
            native.save(run / "task_profile.json", task_route)
            metadata = json.loads((run / "run.json").read_text(encoding="utf-8"))
            metadata.update(execution_kind="nemo_resource_session", actor_provenance="external_unattested",
                            real_model_calls=None, server_model_calls=0, training_eligible=False)
            native.save(run / "run.json", metadata)
            initial = await self.adapter.seed(run) if self.adapter else native.reset(run, archive=False)
            native.save(run / "initial_state.json", initial)
            metadata = json.loads((run / "run.json").read_text(encoding="utf-8"))
            metadata["initial_state_sha256"] = hashlib.sha256((run / "initial_state.json").read_bytes()).hexdigest()
            native.save(run / "run.json", metadata)
            self.ledger.update(sid, state="active")
            state["ready"] = True
            return {"resources_session_id": sid}
        if route == "/close_session":
            sid = session_id(body.get("resources_session_id"))
            state = self.sessions.get(sid)
            if state is not None and state["binding"]["episode_id"] != body.get("episode_id"):
                raise Conflict("Close episode mismatch")
            if state is None:
                if len(self.ledger.rows) >= MAX_SESSIONS:
                    raise Conflict("Session tombstone budget exhausted")
                self.ledger.close_unknown(sid, body)
                self.sessions[sid] = {"closed": True, "binding": body}
            elif not state["closed"]:
                # Keep the state open if cleanup fails, so close can be retried.
                self.ledger.update(sid, state="closing")
                if "run" in state:
                    native.save(state["run"] / "trace.json", state["trace"])
                    if self.adapter: await self.adapter.close(state["run"])
                    else: native.reset(state["run"], archive=True)
                self.ledger.update(sid, state="closed")
                state["closed"] = True
                self.active = None
                native.save(state["run"] / "nemo_closed.json", {"resources_session_id": sid, "closed_at": native.now()})
            return {"resources_session_id": sid}
        sid, state = self.get(body)
        self.ledger.update(sid, state="active")
        run = state["run"]
        if route == "/verify":
            state["verify_count"] = state.get("verify_count", 0) + 1
            if state["verify_count"] > 10:
                raise Conflict("Verification budget exhausted")
            if self.adapter:
                result, evaluation_path = await self.adapter.verify()
            else:
                result = native.evaluate(run)
                retained = run / ("verify_" + uuid.uuid4().hex)
                retained.mkdir()
                for name in ("evaluation.json", "evaluation_original.stdout.txt", "evaluation_original.stderr.txt"):
                    shutil.copyfile(run / name, retained / name)
                evaluation_path = retained / "evaluation.json"
            valid = result["evaluation_status"] == "completed" and type(result["task_passed"]) is bool
            return {"reward": float(result["task_passed"]) if valid else None,
                    "mask_sample": not valid, "evaluation": result,
                    "reward_version": result.get("reward_version", "toolathlon-original-and-supplemental-v1"),
                    "evaluation_ref": evaluation_path.relative_to(native.ROOT).as_posix(),
                    "resources_session_id": sid, "run_ref": run.relative_to(native.ROOT).as_posix()}
        if route == "/tools":
            if self.adapter: return {**self.adapter.tools(), "task_profile": state["task_route"]}
            result = []
            for server, names in ALLOWED.items():
                async with native.mcp_client(server, run) as client:
                    tools = await client.list_tools()
                    result.extend({"server": server, **t.model_dump(mode="json")} for t in tools.tools if t.name in names)
            return {"tools": result, "task_profile": state["task_route"], "artifact_tool": "write_report",
                    "artifact_tools": [{"name": "write_workbook", "inputSchema": PARAMETERS,
                                        "description": "Create the required XLSX report from literal sheet cells"}]}
        if route == "/read_workbook" and self.adapter:
            return {"result": await self.adapter.read()}
        if route not in ("/tool", "/write_report", "/write_workbook"):
            raise ValueError("Unknown resource route")
        if self.adapter and route == "/write_report": raise ValueError("Meal resources accept only the bounded workbook tables")
        request_id = session_id(body.get("request_id"))
        fingerprint = hashlib.sha256(canonical({"route": route, "body": body})).hexdigest()
        if request_id in state["requests"]:
            old = state["requests"][request_id]
            if old["fingerprint"] != fingerprint or "result" not in old:
                raise Conflict("Request identity reused or mutation outcome unknown; do not replay")
            return old["result"]
        if len(state["requests"]) >= 100:
            raise Conflict("Tool-call budget exhausted")
        request = {"fingerprint": fingerprint, "status": "in_progress"}
        state["requests"][request_id] = request
        if self.adapter:
            if route == "/tool":
                call_request = getattr(self.adapter, "call_request", None)
                if callable(call_request):
                    result = await call_request(body.get("server"), body.get("name"), body.get("arguments"), request_id, state["trace"])
                else:
                    result = await self.adapter.call(body.get("server"), body.get("name"), body.get("arguments"), state["trace"])
            else: result = await self.adapter.write(body.get("workbook"))
        elif route == "/tool":
            server, name, arguments = body.get("server"), body.get("name"), body.get("arguments")
            if server not in ALLOWED or name not in ALLOWED[server] or not isinstance(arguments, dict):
                raise ValueError("Tool is outside the original task allowlist")
            async with native.mcp_client(server, run) as client:
                result = await native.call(client, name, arguments, state["trace"])
        else:
            raw = render_workbook(body.get("workbook")) if route == "/write_workbook" else base64.b64decode(body.get("base64", ""), validate=True)
            if not 0 < len(raw) <= 2 * 1024 * 1024:
                raise ValueError("Report must be between 1 byte and 2 MiB")
            path = run / "workspace/Tax_Summary_Report.xlsx"
            from workenv.core import no_links
            no_links(path, native.ROOT)
            path.write_bytes(raw)
            result = {"path": "Tax_Summary_Report.xlsx", "sha256": hashlib.sha256(raw).hexdigest()}
            state["trace"].append({"type": "artifact_write", **result})
        request.update(status="completed", result={"result": result})
        native.save(run / "trace.json", state["trace"])
        return request["result"]


def serve(port=18701, lease_seconds=900):
    token = os.environ["WORKENV_BRIDGE_TOKEN"]
    if len(token) < 32:
        raise ValueError("Use a private random bridge token of at least 32 characters")
    loop = asyncio.new_event_loop()
    resources = Resources(ResourceLedger(native.ROOT / ".runtime/nemo-vm/resource-sessions.json", native.ROOT,
                                         ttl_seconds=lease_seconds))
    synthetic = SyntheticResources()
    isolated = IsolatedGateway(lease_seconds=lease_seconds, task_router=TaskRouter(native.ROOT, "isolated"))
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # No arguments or secrets in HTTP access logs.

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_error(401)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY:
                    raise ValueError("Invalid resource request size")
                self.connection.settimeout(10)
                body = json.loads(self.rfile.read(size))
                if self.path == "/shutdown":
                    isolated.close_all()
                    synthetic.close_all()
                    if resources.active is not None:
                        state = resources.sessions[resources.active]
                        close = {"resources_session_id": resources.active, "episode_id": state["binding"]["episode_id"]}
                        asyncio.run_coroutine_threadsafe(resources.dispatch("/close_session", close), loop).result(timeout=180)
                    result = {"shutdown": "requested"}
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif self.path == "/status":
                    result = {"resource_backend": "original_toolathlon", "active_session": resources.active,
                              "sessions": len(resources.sessions), "real_model_calls": 0,
                              "pid": os.getpid(), "loaded_source_sha256": LOADED_SOURCE_SHA256}
                    result["synthetic"] = synthetic.status()
                    result["isolated"] = isolated.status()
                elif self.path.startswith("/synthetic/"):
                    result = synthetic.dispatch(self.path.removeprefix("/synthetic"), body)
                elif self.path.startswith("/isolated/"):
                    result = isolated.dispatch(self.path.removeprefix("/isolated"), body)
                else:
                    future = asyncio.run_coroutine_threadsafe(resources.dispatch(self.path, body), loop)
                    result = future.result(timeout=180)
                status = 200
            except (Conflict, SyntheticConflict, IsolatedConflict) as exc:
                status, result = 409, {"error": str(exc)}
            except (ValueError, TypeError, KeyError) as exc:
                status, result = 400, {"error": str(exc)[:300]}
            except Exception as exc:
                status, result = 503, {"error": type(exc).__name__, "mask_sample": True, "reward": None}
            raw = wire_json(result)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    try:
        with native.task_lock(), ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
            with native.database(): pass
            asyncio.run_coroutine_threadsafe(resources.recover(), loop).result(timeout=180)
            synthetic.recover()
            isolated.recover()
            reaper = asyncio.run_coroutine_threadsafe(resources.reaper(), loop)
            async def reap_synthetic():
                while True:
                    await asyncio.sleep(15)
                    try: synthetic.reap()
                    except Exception as exc:
                        print(json.dumps({"synthetic_reaper_error": type(exc).__name__}), flush=True)
            synthetic_reaper = asyncio.run_coroutine_threadsafe(reap_synthetic(), loop)
            async def reap_isolated():
                while True:
                    await asyncio.sleep(15)
                    try: await asyncio.to_thread(isolated.reap)
                    except Exception as exc:
                        print(json.dumps({"isolated_reaper_error": type(exc).__name__}), flush=True)
            isolated_reaper = asyncio.run_coroutine_threadsafe(reap_isolated(), loop)
            print(json.dumps({"status": "ready", "port": port, "resource_backend": "original_toolathlon"}), flush=True)
            server.serve_forever()
            reaper.cancel()
            synthetic_reaper.cancel()
            isolated_reaper.cancel()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18701)
    parser.add_argument("--lease-seconds", type=int, default=900)
    arguments = parser.parse_args()
    serve(arguments.port, arguments.lease_seconds)
