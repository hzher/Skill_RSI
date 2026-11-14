"""Durable resource identities and leases; never stores model credentials or tool data."""
from __future__ import annotations

import json
import logging
from copy import deepcopy
import math
from pathlib import Path
import time
import uuid

from workenv.atomic_io import replace_atomic
from workenv.core import no_links
from integrations.ledger_rotation import retired_sessions


class ResourceLedger:
    def __init__(self, path: Path, owner: Path, *, ttl_seconds=900, capacity=128, clock=time.time, monotonic_clock=time.monotonic):
        if type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds) or not 1 <= ttl_seconds <= 3600:
            raise ValueError("Resource lease must be 1..3600 seconds")
        self.path, self.owner = path, owner.resolve()
        self.ttl, self.capacity, self.clock = ttl_seconds, capacity, clock
        # Wall time remains durable metadata. Live leases use elapsed time so a
        # host/WSL clock correction cannot silently close an active task.
        # Anchors are deliberately not persisted or reconstructed on restart.
        self.monotonic_clock, self.live_access = monotonic_clock, {}
        self.rows = {}
        no_links(path, self.owner)
        if path.exists():
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Resource identity ledger exceeds 2 MiB")
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema_version") != "nemo-toolathlon-resource-ledger-v1" or value.get("owner") != str(self.owner):
                raise ValueError("Resource ledger ownership/schema mismatch")
            self.rows = value["sessions"]
            if not isinstance(self.rows, dict) or len(self.rows) > capacity:
                raise ValueError("Resource ledger session budget exceeded")
            for sid, row in self.rows.items():
                if str(uuid.UUID(sid)) != sid or row.get("state") not in ("seeding", "active", "closing", "closed"):
                    raise ValueError("Invalid persisted resource identity")
        self.retired = retired_sessions(self.path, self.owner)
        if set(self.rows) & set(self.retired):
            raise ValueError("Current ledger overlaps retired resource identities")

    def closed_rows(self):
        return {**self.retired, **{sid: row for sid, row in self.rows.items() if row["state"] == "closed"}}

    def save(self):
        no_links(self.path, self.owner)
        raw = json.dumps({"schema_version": "nemo-toolathlon-resource-ledger-v1", "owner": str(self.owner), "sessions": self.rows},
                         sort_keys=True, allow_nan=False).encode("utf-8")
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Resource identity ledger exceeds 2 MiB")
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with temporary.open("xb") as target:
                target.write(raw)
                target.flush()
                import os
                os.fsync(target.fileno())
            replace_atomic(temporary, self.path)
        finally:
            if temporary.exists(): temporary.unlink()

    def seed(self, sid, binding):
        if sid in self.retired:
            raise ValueError("Retired resource identity cannot be recreated")
        if sid in self.rows:
            if self.rows[sid]["state"] != "active" or self.rows[sid]["binding"] != binding:
                raise ValueError("Persisted session cannot be recreated or rebound")
            return
        if len(self.rows) >= self.capacity:
            raise ValueError("Persistent session budget exhausted; preserve ledger before an explicit new run namespace")
        self.rows[sid] = {"state": "seeding", "binding": deepcopy(binding), "run_ref": None,
                          "created_unix": self.clock(), "last_access_unix": self.clock()}
        try:
            self.save()
        except Exception:
            del self.rows[sid]
            raise

    def update(self, sid, *, state=None, run_ref=None):
        row = self.rows[sid]
        previous = deepcopy(row)
        if row["state"] == "closed" and state != "closed":
            raise ValueError("Closed resource identity cannot be reopened")
        if state is not None: row["state"] = state
        if run_ref is not None: row["run_ref"] = run_ref
        row["last_access_unix"] = self.clock()
        elapsed_anchor = self.monotonic_clock()
        try:
            self.save()
        except Exception:
            self.rows[sid] = previous
            raise
        if row["state"] == "active": self.live_access[sid] = elapsed_anchor
        else: self.live_access.pop(sid, None)

    def close_unknown(self, sid, binding):
        if sid in self.retired:
            if binding.get("episode_id") != self.retired[sid]["binding"].get("episode_id"):
                raise ValueError("Retired resource close episode mismatch")
            return
        if sid not in self.rows:
            self.seed(sid, binding)
        self.update(sid, state="closed")

    def expired(self):
        now, monotonic_now = self.clock(), self.monotonic_clock()
        def due(sid, row):
            if sid in self.live_access:
                age = monotonic_now - self.live_access[sid]
                return not math.isfinite(age) or age < 0 or age >= self.ttl
            # A loaded ledger has no live anchor: retain conservative rollback
            # handling until the explicit recovery path closes old sessions.
            return now < row["last_access_unix"] or now - row["last_access_unix"] >= self.ttl
        expired = [sid for sid, row in self.rows.items() if row["state"] == "active" and due(sid, row)]
        for sid in expired:
            logging.getLogger(__name__).warning("Resource lease expired: %s", json.dumps({
                "resource_id": sid, "now_unix": now, "last_access_unix": self.rows[sid]["last_access_unix"],
                "ttl_seconds": self.ttl, "clock_rolled_back": now < self.rows[sid]["last_access_unix"],
                "lease_clock": "monotonic" if sid in self.live_access else "persisted_wall_time"}))
        return expired
