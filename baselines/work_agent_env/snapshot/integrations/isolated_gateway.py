"""Durable resource routing to two isolated workers; no agent/model loop."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from integrations.episode_cluster import EpisodeProfile, recover_orphan_cluster
from integrations.episode_worker_client import EpisodeWorkerClient
from integrations.resource_ledger import ResourceLedger
from integrations.task_routing import TaskRouter
from workenv.core import ROOT, ResourceError, contained, no_links, read_json
from workenv.process_identity import process_identity


class IsolatedConflict(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("A canonical resource UUID is required")
    return value


class IsolatedGateway:
    def __init__(self, *, lease_seconds=900, task_router=None):
        self.task_router = task_router
        self.ledger = ResourceLedger(ROOT / ".runtime/nemo-vm/isolated-sessions.json", ROOT, ttl_seconds=lease_seconds)
        self.sessions = {}
        self.lock = threading.RLock()
        self._refresh_status()

    def _refresh_status(self):
        self._status = {"active_sessions": [sid for sid, row in self.ledger.rows.items() if row["state"] != "closed"],
                    "current_identities": len(self.ledger.rows), "retired_identities": len(self.ledger.retired),
                    "resource_slots": 2, "backend": "isolated_original_toolathlon"}

    def _update(self, sid, **kwargs):
        self.ledger.update(sid, **kwargs)
        self._refresh_status()

    def status(self):
        # Status must remain available while a seed starts a child process.
        return deepcopy(self._status)

    def _seed_sql(self, task_id="wc-sales-tax-summary"):
        if task_id == "wc-customer-order-gsheet-email":
            from integrations.vip_seed import validated_seed
            return validated_seed(ROOT)[1]
        if task_id not in ("wc-sales-tax-summary", "howtocook-meal-plan-gcal", "wc-order-analysis-gcal", "wc-order-fulfillment-tracker", "wc-shipping-zone-gsheet"): raise ValueError("Unknown isolated schema task")
        meal = task_id == "howtocook-meal-plan-gcal"
        order = task_id == "wc-order-analysis-gcal"
        shipping = task_id == "wc-shipping-zone-gsheet"
        fulfillment = task_id == "wc-order-fulfillment-tracker"
        record_path = ROOT / (".runtime/nemo-vm/shipping-seed.json" if shipping else ".runtime/nemo-vm/fulfillment-seed.json" if fulfillment else ".runtime/nemo-vm/order-analysis-seed.json" if order else ".runtime/nemo-vm/meal-plan-seed.json" if meal else ".runtime/nemo-vm/isolated-seed.json")
        no_links(record_path, ROOT)
        record = read_json(record_path)
        pin = read_json(ROOT / "configs/mainline_sources.lock.json")["sources"]["toolathlon_gym"]["commit"]
        if record.get("task_commit") != pin: raise ResourceError("Isolated schema seed belongs to another original task version")
        if meal:
            capabilities = read_json(ROOT / "training/toolathlon_seed_capabilities.json")
            if (record.get("task_id") != task_id or record.get("schemas") != ["gcal"] or record.get("schema_only") is not True
                or record.get("task_config_sha256") != capabilities["tasks"][task_id]["config_sha256"]):
                raise ResourceError("Meal task schema seed has another task, configuration or data scope")
        if order:
            capabilities = read_json(ROOT / "training/toolathlon_seed_capabilities.json")
            if (record.get("task_id") != task_id or record.get("schemas") != ["wc", "gcal"]
                or record.get("calendar_rows_included") is not False
                or record.get("task_config_sha256") != capabilities["tasks"][task_id]["config_sha256"]):
                raise ResourceError("Order schema seed has another task or data scope")
        if fulfillment or shipping:
            capabilities = read_json(ROOT / "training/toolathlon_seed_capabilities.json")
            if (record.get("task_id") != task_id or record.get("schemas") != ["wc", "gsheet"]
                or record.get("sheet_rows_included") is not False
                or record.get("task_config_sha256") != capabilities["tasks"][task_id]["config_sha256"]):
                raise ResourceError("Fulfillment seed has another task, configuration or sheet data scope")
        if shipping:
            import re
            if not isinstance(record.get("shipping_configuration_sha256"), str) or not re.fullmatch("[0-9a-f]{64}", record["shipping_configuration_sha256"]):
                raise ResourceError("Shipping configuration binding is missing")
        path = contained(ROOT, record["seed_ref"])
        if not path.is_relative_to(ROOT / ".runtime/native-episodes/seed-cache") or path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Isolated schema seed escaped its owned cache")
        if hashlib.sha256(path.read_bytes()).hexdigest() != record.get("sha256"):
            raise ResourceError("Isolated schema seed bytes changed")
        return path

    def _recover_profile(self, sid, row):
        expected = ".runtime/native-episodes/" + sid
        if row.get("run_ref") not in (None, expected): raise ValueError("Isolated resource profile reference changed")
        base = contained(ROOT, expected)
        marker = base / "cluster_owner.json"
        no_links(marker, ROOT)
        if not marker.exists():
            if (base / "pgdata/postmaster.pid").exists(): raise ResourceError("Unidentified isolated postmaster retained")
            return
        record = read_json(marker)
        profile = EpisodeProfile(ROOT, sid, record["slot"])
        profile.validate_record(record)
        pidfile = profile.data / "postmaster.pid"
        no_links(pidfile, ROOT)
        slotfile = base.parent / ("slot-" + str(profile.slot) + ".json")
        no_links(slotfile, ROOT)
        if slotfile.exists() and read_json(slotfile).get("resource_id") != sid:
            if pidfile.exists(): raise ResourceError("Isolated slot was rebound before its old postmaster stopped")
            return
        lease = base.parent / ("slot-" + str(profile.slot) + ".lock")
        no_links(lease, ROOT)
        deadline = time.monotonic() + 15
        while lease.exists():
            current = read_json(lease)
            if current.get("resource") != lease.relative_to(ROOT).as_posix() or current.get("workspace_owner") != str(ROOT):
                raise ResourceError("Isolated slot lease ownership changed")
            identity = process_identity(current["process"]["pid"])
            if identity["status"] in ("not_found", "exited"): break
            if time.monotonic() >= deadline:
                raise ResourceError("Isolated worker is alive or unknown; retain its slot")
            time.sleep(.2)
        if (profile.data / "postmaster.pid").exists() or lease.exists():
            recover_orphan_cluster(ROOT, slot=profile.slot, resource_id=sid)

    def recover(self):
        # Run only while the bridge owns the exclusive project process lock.
        with self.lock:
            if not self.ledger.path.exists(): self.ledger.save()
            for sid, row in self.ledger.rows.items():
                if row["state"] == "closed": continue
                self._recover_profile(sid, row)
                self._update(sid, state="closed")

    def close(self, sid, episode):
        with self.lock:
            row = self.ledger.rows.get(sid) or self.ledger.retired.get(sid)
            if row is None:
                self.ledger.close_unknown(sid, {"episode_id": deepcopy(episode)})
                self._refresh_status()
                return {"resources_session_id": sid}
            if row["binding"]["episode_id"] != episode: raise IsolatedConflict("Close episode mismatch")
            if row["state"] == "closed": return {"resources_session_id": sid}
            state = self.sessions.get(sid)
        if state is None:
            self._recover_profile(sid, row)
            with self.lock: self._update(sid, state="closed")
        else:
            with state["lock"]:
                with self.lock: self._update(sid, state="closing")
                worker = state["worker"]
                if worker and worker.process.poll() is None:
                    try: worker.call("/close_session", {"resources_session_id": sid, "episode_id": episode})
                    finally: worker.stop()
                self._recover_profile(sid, row)
                with self.lock:
                    self._update(sid, state="closed")
                    self.sessions.pop(sid, None)
        return {"resources_session_id": sid}

    def close_all(self):
        with self.lock: rows = list(self.ledger.rows.items())
        for sid, row in rows:
            if row["state"] != "closed": self.close(sid, row["binding"]["episode_id"])

    def reap(self):
        with self.lock:
            expired = self.ledger.expired()
            rows = list(self.ledger.rows.items())
        for sid in expired: self.close(sid, self.ledger.rows[sid]["binding"]["episode_id"])
        for sid, row in rows:
            if row["state"] == "closing": self.close(sid, row["binding"]["episode_id"])

    def dispatch(self, route, body):
        if not isinstance(body, dict): raise ValueError("Expected a JSON resource object")
        sid = identifier(body.get("resources_session_id"))
        episode = body.get("episode_id")
        if not isinstance(episode, dict) or not episode.get("rollout_id"): raise ValueError("Episode identity required")
        with self.lock:
            expired = sid in self.ledger.expired()
            old_episode = self.ledger.rows[sid]["binding"]["episode_id"] if expired else None
        if expired: self.close(sid, old_episode)
        if route == "/close_session": return self.close(sid, episode)
        with self.lock:
            if route == "/seed_session":
                task = body.get("task_id")
                if (set(body) != {"resources_session_id", "episode_id", "task_id", "task_data"} or not isinstance(task, dict)
                    or task.get("task_id") not in ("wc-sales-tax-summary", "howtocook-meal-plan-gcal", "wc-order-analysis-gcal", "wc-order-fulfillment-tracker", "wc-shipping-zone-gsheet", "wc-customer-order-gsheet-email")
                    or not isinstance(task.get("taskset"), str) or not task["taskset"].startswith("toolathlon_gym:") or body["task_data"]):
                    raise ValueError("Only the pinned original task is accepted")
                old = self.ledger.rows.get(sid) or self.ledger.retired.get(sid)
                if old:
                    if old["state"] != "active" or old["binding"] != body or sid not in self.sessions:
                        raise IsolatedConflict("Resource cannot be recreated or rebound")
                    return {"resources_session_id": sid}
                if len(self.ledger.rows) >= self.ledger.capacity: raise IsolatedConflict("Isolated session identity budget exhausted")
                free = []
                used = {state["slot"] for state in self.sessions.values()}
                for slot in (0, 1):
                    marker = ROOT / ".runtime/native-episodes" / ("slot-" + str(slot) + ".json")
                    no_links(marker, ROOT)
                    if slot not in used and (not marker.exists() or read_json(marker).get("state") == "stopped"):
                        free.append(slot)
                if not free: raise IsolatedConflict("Both isolated resource slots are in use")
                router = self.task_router or TaskRouter(ROOT, "isolated")
                task_route = router.authorize(body)
                seed_sql = self._seed_sql(task["task_id"])
                self.ledger.seed(sid, body)
                self._update(sid, run_ref=".runtime/native-episodes/" + sid)
                state = self.sessions[sid] = {"worker": None, "slot": free[0], "task_route": task_route, "lock": threading.RLock()}
                try:
                    worker = EpisodeWorkerClient(ROOT, slot=free[0], seed_sql=seed_sql, resource_id=sid,
                        on_spawn=lambda child: state.__setitem__("worker", child), task_id=task["task_id"])
                    state["worker"] = worker
                    reply = worker.call(route, body)
                    if reply["status"] != 200: raise ResourceError("Original isolated task seed failed")
                    self._update(sid, state="active")
                    return reply["result"]
                except Exception:
                    self._update(sid, state="closing")
                    raise
            row, state = self.ledger.rows.get(sid), self.sessions.get(sid)
            if row is None or row["state"] != "active" or state is None or row["binding"]["episode_id"] != episode:
                raise IsolatedConflict("Resource is missing, closed, or bound to another episode")
        with state["lock"]:
            with self.lock:
                if self.ledger.rows[sid]["state"] != "active": raise IsolatedConflict("Resource is closing")
                self._update(sid, state="active")
            reply = state["worker"].call(route, body)
            if reply["status"] == 409: raise IsolatedConflict(reply.get("error", "resource conflict"))
            if reply["status"] == 400: raise ValueError(reply.get("error", "invalid resource request"))
            if reply["status"] != 200: raise ResourceError("Isolated resource unavailable; measurement unknown")
            return reply["result"]
