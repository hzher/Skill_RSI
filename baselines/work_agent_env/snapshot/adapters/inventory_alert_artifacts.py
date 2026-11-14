"""Task-bound UTF-8 file transport, with retained intents and no write replay.

No inventory initialization or runtime registration is performed here.
Business correctness belongs to inventory_alert_validation, not this transport.
"""
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from adapters.inventory_alert_validation import FILENAME, MAX_TEXT_BYTES, source_identity
from adapters.meal_plan_artifacts import preserve_bytes
from integrations.episode_cluster import EpisodeCluster
from training.inventory_task_review import TASK_ID
from workenv.atomic_io import replace_atomic
from workenv.core import ResourceError, no_links, save
from workenv.leases import owned_lock

PREFIX = "inventory-artifact-write-"
SCHEMA = "inventory-text-write-v1"
MAX_WRITES = 32


def encode_text(text):
    if not isinstance(text, str) or "\0" in text:
        raise ValueError("Expected UTF-8 text without NUL")
    try:
        raw = text.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Invalid UTF-8 text") from exc
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError("Inventory text exceeds 256 KiB")
    return raw


def workspace_for(root, cluster, *, require_active=True):
    root = Path(root).resolve()
    if not isinstance(cluster, EpisodeCluster) or cluster.profile.owner != root:
        raise ValueError("Foreign inventory artifact resource")
    cluster.require_owner()
    if require_active and not cluster.started:
        raise ResourceError("Inventory artifact resource is not active")
    marker = cluster.profile.base / "inventory-initialized.json"
    no_links(marker, root)
    if not marker.is_file() or marker.stat().st_size > 8192:
        raise ResourceError("Inventory task marker missing or oversized")
    try: initialized = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc: raise ResourceError("Inventory task marker cannot be decoded") from exc
    if (not isinstance(initialized, dict) or initialized.get("state") != "ready" or initialized.get("profile") != cluster.profile.record()
            or initialized.get("task_id") != TASK_ID or initialized.get("contract_source") != source_identity()):
        raise ResourceError("Inventory task marker identity differs")
    workspace = cluster.profile.base / "inventory-workspace"
    no_links(workspace, root)
    if not workspace.is_dir():
        raise ResourceError("Inventory workspace unavailable")
    return workspace


def intent_record(root, cluster, path, target):
    no_links(path, root)
    if not re.fullmatch(PREFIX + r"[0-9a-f]{32}\.json", path.name) or not path.is_file() or path.stat().st_size > 8192:
        raise ResourceError("Inventory intent path or size differs")
    raw = path.read_bytes()
    try: record = json.loads(raw)
    except (ValueError, UnicodeError) as exc: raise ResourceError("Inventory intent cannot be decoded") from exc
    digest = lambda value: isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
    if (not isinstance(record, dict) or record.get("schema_version") != SCHEMA
            or record.get("task_id") != TASK_ID or record.get("contract_source") != source_identity()
            or record.get("profile") != cluster.profile.record()
            or record.get("artifact_ref") != target.relative_to(root).as_posix()
            or record.get("task_content_evaluated") is not False
            or type(record.get("bytes")) is not int or not 0 <= record["bytes"] <= MAX_TEXT_BYTES
            or not digest(record.get("sha256")) or "previous_sha256" not in record
            or record["previous_sha256"] is not None and not digest(record["previous_sha256"])
            or record.get("state") not in ("prepared", "completed", "aborted")):
        raise ResourceError("Inventory intent ownership or byte claims differ")
    return raw, record


def require_no_pending(root, cluster, target):
    count = 0
    for path in cluster.profile.base.glob(PREFIX + "*.json"):
        count += 1
        if count > MAX_WRITES:
            raise ResourceError("Inventory intent history budget exceeded")
        _, record = intent_record(root, cluster, path, target)
        if record["state"] == "prepared":
            raise ResourceError("Inventory artifact outcome is unknown; reconcile without replay")
    return count


def observed(root, target):
    no_links(target, root)
    if not target.exists():
        return None
    if not target.is_file() or target.stat().st_size > MAX_TEXT_BYTES:
        raise ResourceError("Inventory artifact byte budget or file shape differs")
    raw = target.read_bytes()
    if len(raw) > MAX_TEXT_BYTES:
        raise ResourceError("Inventory artifact grew beyond budget")
    return raw


def write_report(root, cluster, text):
    raw = encode_text(text)
    root = Path(root).resolve()
    workspace = workspace_for(root, cluster)
    target = workspace / FILENAME
    with owned_lock(cluster.profile.base / ".inventory-artifact.lock", root, "inventory_artifact"):
        if require_no_pending(root, cluster, target) >= MAX_WRITES:
            raise ResourceError("Inventory write history budget exhausted")
        previous = observed(root, target)
        previous_sha = hashlib.sha256(previous).hexdigest() if previous is not None else None
        if previous is not None:
            preserve_bytes(cluster.profile.base / "inventory-artifact-history" / (previous_sha + ".txt"), previous, root)
        temporary = workspace / (".inventory-text-" + uuid4().hex + ".tmp")
        intent = cluster.profile.base / (PREFIX + uuid4().hex + ".json")
        record = {"schema_version": SCHEMA, "task_id": TASK_ID, "contract_source": source_identity(),
            "profile": cluster.profile.record(), "artifact_ref": target.relative_to(root).as_posix(),
            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "previous_sha256": previous_sha,
            "task_content_evaluated": False}
        try:
            with temporary.open("xb") as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            save(intent, {**record, "state": "prepared"})
            if observed(root, target) != previous:
                raise ResourceError("Inventory artifact changed after preparing the write")
            replace_atomic(temporary, target)
            save(intent, {**record, "state": "completed"})
            return record
        finally:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass


def read_report(root, cluster):
    root = Path(root).resolve()
    target = workspace_for(root, cluster) / FILENAME
    with owned_lock(cluster.profile.base / ".inventory-artifact.lock", root, "inventory_artifact"):
        require_no_pending(root, cluster, target)
        raw = observed(root, target)
        if raw is None:
            raise ResourceError("Inventory artifact missing")
        try: text = raw.decode("utf-8")
        except UnicodeError as exc: raise ResourceError("Inventory artifact is not UTF-8") from exc
        return {"artifact_ref": target.relative_to(root).as_posix(), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "text": text}


def reconcile_write(root, cluster, intent_name):
    root = Path(root).resolve()
    target = workspace_for(root, cluster, require_active=False) / FILENAME
    if not isinstance(intent_name, str) or not re.fullmatch(PREFIX + r"[0-9a-f]{32}\.json", intent_name):
        raise ValueError("Expected an inventory intent basename")
    path = cluster.profile.base / intent_name
    # A live/unknown writer's existing lock remains in place. No process is
    # signalled, and no stale-lock removal is inferred by this component.
    with owned_lock(cluster.profile.base / ".inventory-artifact.lock", root, "inventory_artifact"):
        original, record = intent_record(root, cluster, path, target)
        if record["state"] != "prepared":
            return {"decision": "already_resolved", "state": record["state"], "replay_performed": False}
        raw = observed(root, target)
        checksum = hashlib.sha256(raw).hexdigest() if raw is not None else None
        if checksum == record["sha256"] and raw is not None and len(raw) == record["bytes"]:
            decision, state = "committed", "completed"
        elif checksum == record["previous_sha256"]:
            decision, state = "not_committed", "aborted"
        else:
            raise ResourceError("Inventory artifact matches neither old nor new bytes")
        original_sha = hashlib.sha256(original).hexdigest()
        archive = cluster.profile.base / "inventory-artifact-intent-history" / (original_sha + ".json")
        preserve_bytes(archive, original, root)
        if path.read_bytes() != original or observed(root, target) != raw:
            raise ResourceError("Inventory artifact or intent changed during reconciliation")
        resolution = {"decision": decision, "state": state, "replay_performed": False,
            "original_intent_ref": archive.relative_to(root).as_posix(), "original_intent_sha256": original_sha,
            "observed_sha256": checksum}
        save(path, {**record, "state": state, "reconciliation": resolution})
        return resolution
