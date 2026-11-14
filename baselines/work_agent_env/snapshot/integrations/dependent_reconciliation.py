"""Explicit metadata reconciliation after all registered episode children exit.

No process is terminated, no request is replayed and no database is restarted.
Unidentified children and live/unknown owners keep the original state intact.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from integrations.episode_cluster import EpisodeCluster, EpisodeProfile, require_dependents_closed
from workenv.core import ResourceError, no_links, read_json, save
from workenv.leases import owned_lock
from workenv.process_identity import process_identity

LOCKS = {".meal-services.lock": "meal_plan_business_services", ".recipe.lock": "meal_recipe_episode_mcp",
         ".calendar.lock": "calendar_episode_mcp", ".order-woocommerce.lock": "order_woocommerce_mcp"}
UNKNOWN = {"recipe-process-unknown.json": "howtocook", "calendar-process-unknown.json": "google_calendar", "woocommerce-process-unknown.json": "woocommerce"}


def bounded_raw(path, root):
    no_links(path, root)
    if not path.is_file() or not 0 < path.stat().st_size <= 32 * 1024:
        raise ResourceError("Unexpected dependent reconciliation record size")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict): raise ResourceError("Dependent reconciliation record must be an object")
    return raw, value


def dead_owner(path, root, kind):
    raw, record = bounded_raw(path, root)
    if (record.get("schema_version") != "workenv-lock-v0.1" or record.get("workspace_owner") != str(root)
        or record.get("resource") != path.relative_to(root).as_posix() or record.get("kind") != kind
        or type(record.get("process", {}).get("pid")) is not int or record["process"]["pid"] <= 0
        or not isinstance(record["process"].get("creation_id"), str) or not record["process"]["creation_id"]):
        raise ResourceError("Dependent lease ownership is missing or differs")
    observed = process_identity(record["process"]["pid"])
    if observed["status"] not in ("not_found", "exited"):
        raise ResourceError("Dependent lease owner is alive or unknown; no reconciliation attempted")
    return raw, record


def archive(root, profile, name, raw):
    directory = profile.base / "dependent-record-history"
    no_links(directory, root)
    directory.mkdir(exist_ok=True)
    files = list(directory.iterdir())
    for member in files: no_links(member, root)
    if len(files) >= 128 or sum(p.stat().st_size for p in files) + len(raw) > 4 * 1024 * 1024:
        raise ResourceError("Dependent record history budget exhausted")
    path = directory / (name + "." + hashlib.sha256(raw).hexdigest() + ".json")
    if path.exists():
        if path.read_bytes() != raw: raise ResourceError("Dependent record archive differs")
    else:
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    return path.relative_to(root).as_posix()


def reconcile_dependents(root, *, resource_id, slot, recover_stale_locks=False):
    root = Path(root).resolve()
    profile = EpisodeProfile(root, resource_id, slot)
    EpisodeCluster(profile).require_owner()
    slotfile = profile.base.parent / ("slot-" + str(slot) + ".json")
    slot_raw, slot_record = bounded_raw(slotfile, root)
    if (slot_record.get("workspace_owner") != str(root) or slot_record.get("resource_id") != resource_id
        or type(slot_record.get("slot")) is not int or slot_record["slot"] != slot
        or slot_record.get("state") not in ("reserved", "unknown", "stopped")):
        raise ResourceError("Dependent reconciliation belongs to another resource slot")
    slot_lock = profile.base.parent / ("slot-" + str(slot) + ".lock")
    no_links(slot_lock, root)
    if slot_lock.exists(): dead_owner(slot_lock, root, "native_episode_slot")
    path = profile.base / "dependent_processes.json"
    raw, record = bounded_raw(path, root)
    if record.get("schema_version") != "native-episode-dependents-v1" or record.get("profile") != profile.record():
        raise ResourceError("Dependent process record belongs to another episode")
    if record.get("state") == "closed":
        try: require_dependents_closed(profile)
        except ResourceError: pass  # Retained unknown flags still need explicit reconciliation.
        else:
            return {"outcome": "already_resolved", "profile": profile.record(), "records_modified": 0,
                    "processes_terminated": 0, "requests_replayed": 0, "slot_released": False}
    expected, children = record.get("expected_children"), record.get("children")
    if (record.get("state") not in ("opening", "open", "unknown", "closed") or not isinstance(expected, list)
        or not 1 <= len(expected) <= 16 or any(not isinstance(n, str) or not 1 <= len(n) <= 100 for n in expected)
        or len(set(expected)) != len(expected) or not isinstance(children, dict) or set(children) != set(expected)):
        raise ResourceError("Incomplete dependent startup cannot be inferred as a complete child inventory")
    resolved = {}
    for name, child in children.items():
        if not isinstance(child, dict): raise ResourceError("Malformed dependent child record")
        before = child.get("before", {})
        if (not isinstance(before, dict) or child.get("profile") != profile.record() or before.get("status") != "alive"
            or type(before.get("pid")) is not int or before["pid"] <= 0
            or not isinstance(before.get("creation_id"), str) or not before["creation_id"]):
            raise ResourceError("Dependent child lacks an attested original process identity")
        current = process_identity(before["pid"])
        closed = current["status"] in ("not_found", "exited") or bool(current["status"] == "alive"
            and current.get("creation_id") and current["creation_id"] != before["creation_id"])
        if not closed: raise ResourceError("Dependent child is alive or unknown; no reconciliation attempted")
        resolved[name] = {**child, "after": current, "closed": True}
    removals = []
    for name, kind in LOCKS.items():
        member = profile.base / name
        no_links(member, root)
        if member.exists():
            if not recover_stale_locks: raise ResourceError("Stale component records require explicit --recover-stale-locks")
            original, _ = dead_owner(member, root, kind)
            removals.append((member, original, kind))
    for name, child_name in UNKNOWN.items():
        member = profile.base / name
        no_links(member, root)
        if member.exists():
            if not recover_stale_locks: raise ResourceError("Unknown component records require explicit --recover-stale-locks")
            original, unknown = bounded_raw(member, root)
            if (child_name not in children or unknown.get("profile") != profile.record()
                or unknown.get("before") != children[child_name]["before"]):
                raise ResourceError("Unknown component record has a different child identity")
            removals.append((member, original, None))
    with owned_lock(profile.base / ".dependent-reconciliation.lock", root, "episode_dependent_reconciliation"):
        if path.read_bytes() != raw or slotfile.read_bytes() != slot_raw:
            raise ResourceError("Dependent state or slot changed during reconciliation")
        if slot_lock.exists(): dead_owner(slot_lock, root, "native_episode_slot")
        archives = [archive(root, profile, path.name, raw)]
        for member, original, kind in removals:
            if member.read_bytes() != original: raise ResourceError("Dependent component record changed during reconciliation")
            if kind: dead_owner(member, root, kind)
            archives.append(archive(root, profile, member.name, original))
        # Removing a stale flag cannot release a slot while the original dependent
        # record remains unresolved. The closed record is committed last.
        for member, original, kind in removals:
            if member.read_bytes() != original: raise ResourceError("Dependent component record was replaced")
            if kind: dead_owner(member, root, kind)
            member.unlink()
        if path.read_bytes() != raw or slotfile.read_bytes() != slot_raw:
            raise ResourceError("Dependent state changed before commit; original archives retained")
        updated = {**record, "state": "closed", "children": resolved, "reconciliation": {
            "original_sha256": hashlib.sha256(raw).hexdigest(), "at": datetime.now(timezone.utc).isoformat(),
            "archives": archives, "processes_terminated": 0, "requests_replayed": 0}}
        save(path, updated)
        require_dependents_closed(profile)
    return {"outcome": "children_confirmed_closed", "profile": profile.record(), "records_modified": 1,
            "archives": archives, "processes_terminated": 0, "requests_replayed": 0, "slot_released": False}
