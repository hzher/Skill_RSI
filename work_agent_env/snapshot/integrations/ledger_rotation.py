"""Offline ledger generations retain old closed identities in immutable archives."""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from workenv.atomic_io import replace_atomic
from workenv.core import no_links
from workenv.leases import owned_lock
from workenv.process_identity import process_identity

MAX_BYTES = 2 * 1024 * 1024
MAX_HISTORY_BYTES = 16 * 1024 * 1024


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_owned(path, owner):
    no_links(path, owner)
    if path.stat().st_size > MAX_BYTES: raise ValueError("Ledger metadata exceeds 2 MiB")
    return path.read_bytes()


def locations(path, owner):
    path, owner = Path(path).absolute(), Path(owner).resolve()
    no_links(path, owner)
    base = path.parent / "ledger-history" / path.stem
    no_links(base, owner)
    return path.with_name(path.stem + ".history.json"), base, base / "rotation-pending.json"


def ledger_value(raw, owner):
    value = json.loads(raw)
    if value.get("schema_version") != "nemo-toolathlon-resource-ledger-v1" or value.get("owner") != str(owner):
        raise ValueError("Archived ledger ownership/schema mismatch")
    rows = value.get("sessions")
    if not isinstance(rows, dict) or len(rows) > 128: raise ValueError("Archived ledger session budget exceeded")
    for sid, row in rows.items():
        if str(uuid.UUID(sid)) != sid or row.get("state") != "closed" or not isinstance(row.get("binding"), dict):
            raise ValueError("Rotation requires all identities to be closed")
    return rows


def history_value(path, owner):
    index, _, _ = locations(path, owner)
    no_links(index, owner)
    value = json.loads(read_owned(index, owner)) if index.exists() else {
        "schema_version": "nemo-resource-ledger-history-v1", "owner": str(owner), "ledger_ref": path.relative_to(owner).as_posix(), "archives": []}
    if (value.get("schema_version") != "nemo-resource-ledger-history-v1" or value.get("owner") != str(owner)
        or value.get("ledger_ref") != path.relative_to(owner).as_posix() or not isinstance(value.get("archives"), list)
        or len(value["archives"]) > 32):
        raise ValueError("Invalid/bounded ledger history manifest")
    return value


def retired_sessions(path, owner):
    path, owner = Path(path).absolute(), Path(owner).resolve()
    _, base, pending = locations(path, owner)
    no_links(pending, owner)
    if pending.exists() and json.loads(read_owned(pending, owner)).get("state") not in ("completed", "rolled_back"):
        raise ValueError("Unfinished ledger rotation requires explicit recovery before startup")
    history = history_value(path, owner)
    rows, total = {}, 0
    for entry in history["archives"]:
        checksum = entry.get("sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", checksum): raise ValueError("Invalid ledger archive checksum")
        archive = base / (checksum + ".json")
        raw = read_owned(archive, owner)
        total += len(raw)
        if total > MAX_HISTORY_BYTES or digest(raw) != checksum: raise ValueError("Ledger archive changed or exceeds history budget")
        previous = ledger_value(raw, owner)
        if set(rows) & set(previous): raise ValueError("Retired resource identity appears in multiple generations")
        rows.update(previous)
    if len(rows) > 4096: raise ValueError("Retired resource identity budget exceeded")
    return rows


def require_offline(owner):
    marker = owner / ".runtime/native_task.lock"
    no_links(marker, owner)
    if marker.exists(): raise RuntimeError("Stop the owned idle resource bridge before rotating a ledger")
    process_file = owner / ".runtime/nemo-vm/bridge-process.json"
    no_links(process_file, owner)
    if process_file.exists():
        process = json.loads(read_owned(process_file, owner))
        if process_identity(process["pid"])["status"] not in ("not_found", "exited"):
            raise RuntimeError("Resource bridge process is alive or unknown; rotation refused")


def rotation_plan(path, owner):
    path, owner = Path(path).absolute(), Path(owner).resolve()
    raw = read_owned(path, owner)
    rows = ledger_value(raw, owner)
    if not rows: raise ValueError("An empty generation does not need rotation")
    previous = retired_sessions(path, owner)
    if set(rows) & set(previous): raise ValueError("Current ledger reuses a retired identity")
    history = history_value(path, owner)
    if len(history["archives"]) >= 32: raise ValueError("Ledger archive generation budget exhausted")
    index, base, _ = locations(path, owner)
    historical_bytes = sum((base / (entry["sha256"] + ".json")).stat().st_size for entry in history["archives"])
    if historical_bytes + len(raw) > MAX_HISTORY_BYTES or len(previous) + len(rows) > 4096:
        raise ValueError("Next generation would exceed the retained history budget")
    return {"schema_version": "resource-ledger-rotation-plan-v1", "ledger_ref": path.relative_to(owner).as_posix(),
            "expected_sha256": digest(raw), "archive_ref": (base / (digest(raw) + ".json")).relative_to(owner).as_posix(),
            "history_ref": index.relative_to(owner).as_posix(), "closing_identities_retained": len(rows),
            "previously_retired_identities": len(previous), "next_generation": len(history["archives"]) + 1,
            "new_current_identity_budget": 128, "old_identity_reactivation_allowed": False,
            "requires_stopped_owned_bridge": True, "dry_run": True}


def immutable(path, raw, owner):
    no_links(path, owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if read_owned(path, owner) != raw: raise ValueError("Immutable ledger archive collision")
        return
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def publish(path, raw, owner):
    no_links(path, owner)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        replace_atomic(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def recover_rotation(path, owner):
    path, owner = Path(path).absolute(), Path(owner).resolve()
    require_offline(owner)
    index, base, pending = locations(path, owner)
    with owned_lock(base / "rotation.lock", owner, "ledger_rotation"):
        tx = json.loads(read_owned(pending, owner))
        if (tx.get("schema_version") != "resource-ledger-rotation-transaction-v1" or not re.fullmatch(r"[0-9a-f]{32}", tx.get("transaction_id", ""))
            or tx.get("ledger_ref") != path.relative_to(owner).as_posix() or tx.get("state") not in ("pending", "completed", "rolled_back")):
            raise ValueError("Invalid pending rotation identity/state")
        if tx.get("state") in ("completed", "rolled_back"): return tx
        for field in ("old_ledger_sha256", "new_ledger_sha256", "new_history_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", tx.get(field, "")): raise ValueError("Invalid pending rotation checksum")
        if tx.get("ledger_ref") != path.relative_to(owner).as_posix(): raise ValueError("Pending rotation ledger changed")
        old_index_sha = tx.get("old_history_sha256")
        if old_index_sha is not None and not re.fullmatch(r"[0-9a-f]{64}", old_index_sha): raise ValueError("Invalid previous index checksum")
        current_ledger = digest(read_owned(path, owner))
        current_index = digest(read_owned(index, owner)) if index.exists() else None
        if current_ledger not in (tx["old_ledger_sha256"], tx["new_ledger_sha256"]) or current_index not in (old_index_sha, tx["new_history_sha256"]):
            raise ValueError("Rotation bytes changed outside the transaction; preserve for diagnosis")
        if current_ledger == tx["new_ledger_sha256"] and current_index == tx["new_history_sha256"]:
            tx["state"] = "completed"
        else:
            old_raw = read_owned(base / (tx["old_ledger_sha256"] + ".json"), owner)
            if digest(old_raw) != tx["old_ledger_sha256"]: raise ValueError("Rollback ledger archive changed")
            ledger_value(old_raw, owner)
            old_index = None
            if old_index_sha is not None:
                old_index = read_owned(base / ("index-" + old_index_sha + ".json"), owner)
                if digest(old_index) != old_index_sha: raise ValueError("Rollback history archive changed")
            publish(path, old_raw, owner)
            if old_index_sha is None:
                if index.exists(): index.unlink()
            else:
                publish(index, old_index, owner)
            tx["state"] = "rolled_back"
        publish(pending, encoded(tx), owner)
        immutable(base / ("rotation-" + tx["transaction_id"] + ".json"), encoded(tx), owner)
        return tx


def rotate_ledger(path, owner, *, expected_sha256):
    path, owner = Path(path).absolute(), Path(owner).resolve()
    require_offline(owner)
    plan = rotation_plan(path, owner)
    if plan["expected_sha256"] != expected_sha256: raise ValueError("Ledger changed after planning")
    index, base, pending = locations(path, owner)
    base.mkdir(parents=True, exist_ok=True)
    with owned_lock(base / "rotation.lock", owner, "ledger_rotation"):
        old_raw = read_owned(path, owner)
        if digest(old_raw) != expected_sha256: raise ValueError("Ledger changed before rotation")
        history = history_value(path, owner)
        old_index = read_owned(index, owner) if index.exists() else None
        immutable(base / (expected_sha256 + ".json"), old_raw, owner)
        if old_index is not None: immutable(base / ("index-" + digest(old_index) + ".json"), old_index, owner)
        new_history = encoded({**history, "archives": [*history["archives"], {"sha256": expected_sha256}]})
        new_ledger = encoded({"schema_version": "nemo-toolathlon-resource-ledger-v1", "owner": str(owner), "sessions": {}})
        tx = {"schema_version": "resource-ledger-rotation-transaction-v1", "transaction_id": uuid.uuid4().hex,
              "ledger_ref": path.relative_to(owner).as_posix(), "state": "pending", "old_ledger_sha256": expected_sha256,
              "new_ledger_sha256": digest(new_ledger), "old_history_sha256": digest(old_index) if old_index is not None else None,
              "new_history_sha256": digest(new_history)}
        publish(pending, encoded(tx), owner)
        # A crash between these writes leaves a pending marker and blocks startup.
        publish(index, new_history, owner)
        publish(path, new_ledger, owner)
        tx["state"] = "completed"
        publish(pending, encoded(tx), owner)
        immutable(base / ("rotation-" + tx["transaction_id"] + ".json"), encoded(tx), owner)
    retired_sessions(path, owner)
    return {**plan, "dry_run": False, "state": "completed", "transaction_id": tx["transaction_id"]}
