"""Task-limited workbook transport for an already initialized calendar episode.

Only structure and budgets are checked here. Incorrect task content is writable
so that the original and supplemental evaluators can measure it independently.
"""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
from uuid import uuid4

from integrations.episode_cluster import EpisodeCluster
from workenv.atomic_io import replace_atomic
from workenv.core import ResourceError, no_links, save
from workenv.leases import owned_lock
from workenv.process_identity import process_identity

MAX_BYTES = 1024 * 1024
FIELDS = {"meal_plan": ("day", "meal_type", "recipe_name", "prep_time"), "shopping_list": ("ingredient", "quantity")}


def artifact_layout(task="meal"):
    if task == "shipping":
        from adapters.shipping_validation import HEADERS
        return {"prefix": "shipping", "schema": "shipping", "filename": "Shipping_Analysis.xlsx",
                "workspace": "shipping-workspace", "marker": "shipping-initialized.json", "fields": HEADERS,
                "sheets": {name: name for name in HEADERS}, "lock_kind": "shipping_artifact"}
    if task == "fulfillment":
        from adapters.fulfillment_validation import HEADERS
        return {"prefix": "fulfillment", "schema": "fulfillment", "filename": "Fulfillment_Report.xlsx",
                "workspace": "fulfillment-workspace", "marker": "fulfillment-initialized.json", "fields": HEADERS,
                "sheets": {name: name for name in HEADERS}, "lock_kind": "fulfillment_artifact"}
    if task == "customer_report":
        from adapters.customer_spending_validation import HEADERS
        return {"prefix": "customer-report", "schema": "customer-report", "filename": "WC_Customer_Report.xlsx",
                "workspace": "customer-report-workspace", "marker": "customer-report-initialized.json", "fields": HEADERS,
                "sheets": {name: name for name in HEADERS}, "lock_kind": "customer_report_artifact"}
    if task == "meal":
        return {"prefix": "meal", "schema": "meal-plan", "filename": "Weekly_Meal_Plan.xlsx",
                "workspace": "calendar-workspace", "marker": "calendar-initialized.json", "fields": FIELDS,
                "sheets": {"meal_plan": "Meal Plan", "shopping_list": "Shopping List"}, "lock_kind": "meal_plan_artifact"}
    if task == "order_analysis":
        from adapters.order_analysis_validation import HEADERS
        return {"prefix": "order-analysis", "schema": "order-analysis", "filename": "Order_Analysis.xlsx",
                "workspace": "order-analysis-workspace", "marker": "order-analysis-initialized.json", "fields": HEADERS,
                "sheets": {name: name for name in HEADERS}, "lock_kind": "order_analysis_artifact"}
    raise ValueError("Unknown task-limited workbook layout")


def validate_order_payload(payload):
    fields = artifact_layout("order_analysis")["fields"]
    if not isinstance(payload, dict) or set(payload) != set(fields): raise ValueError("Expected the two order-analysis tables only")
    try: raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc: raise ValueError("Workbook payload must be finite JSON") from exc
    if len(raw) > 256 * 1024: raise ValueError("Workbook payload exceeds 256 KiB")
    for table, columns in fields.items():
        rows = payload[table]
        if not isinstance(rows, list) or len(rows) > (256 if table == "Status_Summary" else 4096): raise ValueError("Workbook row budget exceeded")
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(columns): raise ValueError("Unexpected workbook row fields")
            for key, value in row.items():
                if key in ("Status", "Product_Name", "SKU"):
                    if value is None and key == "SKU": continue
                    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 and c not in "\n\t\r" for c in value): raise ValueError("Workbook text is not bounded printable text")
                elif isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 10**12 or not math.isfinite(value):
                    raise ValueError("Workbook number must be finite and bounded")
    return hashlib.sha256(raw).hexdigest()


def validate_customer_payload(payload, *, task="customer_report"):
    if task not in ("customer_report", "fulfillment", "shipping"): raise ValueError("Unsupported office table layout")
    fields = artifact_layout(task)["fields"]
    if not isinstance(payload, dict) or set(payload) != set(fields): raise ValueError("Expected fixed customer tables")
    try: raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc: raise ValueError("Workbook must be finite JSON") from exc
    if len(raw) > 256 * 1024: raise ValueError("Workbook payload exceeds budget")
    for name, columns in fields.items():
        rows = payload[name]
        limit = 4096 if name in ("Customer Spending", "Methods") else 256 if name == "Order Status" else 16
        if not isinstance(rows, list) or len(rows) > limit: raise ValueError("Office table row budget")
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(columns): raise ValueError("Customer table columns differ")
            for key, value in row.items():
                if isinstance(value, str):
                    if len(value) > 4096 or any(ord(c) < 32 and c not in "\t\r\n" for c in value): raise ValueError("Workbook text budget")
                elif key in ("Name", "Email", "Metric", "Status", "Zone_Name", "Zone", "Method", "Details") or isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 10**12 or not math.isfinite(value):
                    raise ValueError("Workbook cells must be bounded literals")
    return hashlib.sha256(raw).hexdigest()


def validate_payload(payload):
    if not isinstance(payload, dict) or set(payload) != set(FIELDS): raise ValueError("Expected only meal_plan and shopping_list tables")
    try: encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc: raise ValueError("Workbook payload must be finite JSON") from exc
    if len(encoded) > 256 * 1024: raise ValueError("Workbook payload exceeds 256 KiB")
    for table, fields in FIELDS.items():
        rows = payload[table]
        if not isinstance(rows, list) or len(rows) > (128 if table == "meal_plan" else 4096): raise ValueError("Workbook row budget exceeded")
        for row in rows:
            if (not isinstance(row, dict) or not set(fields) <= set(row)
                or not set(row) <= set(fields) | ({"recipe_id"} if table == "meal_plan" else set())):
                raise ValueError("Unexpected workbook row fields")
            for key, value in row.items():
                if key == "prep_time":
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 10**12 or not math.isfinite(value): raise ValueError("Prep_Time must be finite bounded numeric minutes")
                elif key == "quantity" and isinstance(value, (int, float)) and not isinstance(value, bool):
                    if abs(value) > 10**12 or not math.isfinite(value): raise ValueError("Quantity must be finite and bounded")
                elif not isinstance(value, str) or len(value) > 512 or any(ord(char) < 32 and char not in "\n\t\r" for char in value):
                    raise ValueError("Workbook text must be bounded printable strings")
    return hashlib.sha256(encoded).hexdigest()


def workspace_for(root, cluster, *, require_active=True, task="meal"):
    layout = artifact_layout(task)
    root = Path(root).resolve()
    if not isinstance(cluster, EpisodeCluster) or cluster.profile.owner != root: raise ValueError("Foreign workbook episode")
    cluster.require_owner()
    if require_active and not cluster.started: raise ResourceError("Workbook episode cluster is not active")
    marker = cluster.profile.base / layout["marker"]
    no_links(marker, root)
    initialized = json.loads(marker.read_text(encoding="utf-8"))
    if initialized.get("state") != "ready" or initialized.get("profile") != cluster.profile.record():
        raise ResourceError("Workbook episode initialization is not ready")
    if task == "order_analysis" and initialized.get("task_id") != "wc-order-analysis-gcal":
        raise ResourceError("Workbook task initialization identity differs")
    if task == "customer_report" and initialized.get("task_id") != "wc-customer-spending-report":
        raise ResourceError("Workbook task initialization identity differs")
    if task == "fulfillment" and initialized.get("task_id") != "wc-order-fulfillment-tracker":
        raise ResourceError("Workbook task initialization identity differs")
    if task == "shipping" and initialized.get("task_id") != "wc-shipping-zone-gsheet":
        raise ResourceError("Workbook task initialization identity differs")
    workspace = cluster.profile.base / layout["workspace"]
    no_links(workspace, root)
    if not workspace.is_dir(): raise ResourceError("Owned calendar workspace is missing")
    return workspace


def require_no_pending_writes(cluster, *, task="meal"):
    layout = artifact_layout(task)
    intents = list(cluster.profile.base.glob(layout["prefix"] + "-artifact-write-*.json"))
    if len(intents) > 128: raise ResourceError("Workbook write history exceeds its budget")
    for path in intents:
        no_links(path, cluster.profile.owner)
        if path.stat().st_size > 8192: raise ResourceError("Unexpected workbook intent size")
        try: record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc: raise ResourceError("Workbook intent is unreadable") from exc
        if not isinstance(record, dict) or record.get("profile") != cluster.profile.record() or record.get("state") not in ("completed", "aborted"):
            raise ResourceError("Previous workbook write is unresolved; preserve it for diagnosis")
    return len(intents)


def write_report(root, cluster, payload, *, task="meal"):
    layout = artifact_layout(task)
    root = Path(root).resolve()
    workspace = workspace_for(root, cluster, task=task)
    payload_sha256 = (validate_payload(payload) if task == "meal" else validate_customer_payload(payload, task=task)
                      if task in ("customer_report", "fulfillment", "shipping") else validate_order_payload(payload))
    try: from openpyxl import Workbook
    except ImportError as exc: raise ResourceError("Workbook dependency is unavailable") from exc
    target = workspace / layout["filename"]
    no_links(target, root)
    with owned_lock(cluster.profile.base / ("." + layout["prefix"] + "-artifact.lock"), root, layout["lock_kind"]):
        if require_no_pending_writes(cluster, task=task) >= 128: raise ResourceError("Workbook write history budget reached")
        previous_sha256 = None
        if target.exists():
            if not target.is_file() or target.stat().st_size > MAX_BYTES: raise ResourceError("Existing workbook exceeds its artifact budget")
            raw = target.read_bytes()
            previous_sha256 = hashlib.sha256(raw).hexdigest()
            archive = cluster.profile.base / (layout["prefix"] + "-artifact-history") / (previous_sha256 + ".xlsx")
            no_links(archive, root)
            archive.parent.mkdir(exist_ok=True)
            history = list(archive.parent.iterdir())
            for member in history: no_links(member, root)
            if (any(not member.is_file() or member.suffix != ".xlsx" for member in history)
                or (not archive.exists() and (len(history) >= 64 or sum(p.stat().st_size for p in history) + len(raw) > 16 * MAX_BYTES))):
                raise ResourceError("Workbook history budget reached or contains unexpected members")
            if archive.exists():
                if archive.read_bytes() != raw: raise ResourceError("Retained previous workbook was changed")
            else:
                with archive.open("xb") as stream: stream.write(raw)
        temporary = workspace / ("." + layout["prefix"] + "-workbook-" + uuid4().hex + ".xlsx")
        workbook = Workbook()
        try:
            for i, (table, fields) in enumerate(layout["fields"].items()):
                sheet = workbook.active if i == 0 else workbook.create_sheet()
                sheet.title = layout["sheets"][table]
                columns = [*fields, *(["recipe_id"] if table == "meal_plan" and any("recipe_id" in r for r in payload[table]) else [])]
                sheet.append([key if task != "meal" else key.title() if key in ("day", "ingredient", "quantity") else "_".join(p.title() for p in key.split("_")) for key in columns])
                for row in payload[table]:
                    sheet.append([row.get(key) for key in columns])
                    # Recipe and ingredient names remain literal strings even
                    # when their first character happens to be '='.
                    for cell in sheet[sheet.max_row]:
                        if isinstance(cell.value, str): cell.data_type = "s"
            workbook.save(temporary)
            if temporary.stat().st_size > MAX_BYTES: raise ValueError("Workbook output exceeds 1 MiB")
            raw = temporary.read_bytes()
            record = {"schema_version": layout["schema"] + "-artifact-write-v1", "profile": cluster.profile.record(),
                "artifact_ref": target.relative_to(root).as_posix(), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                "payload_sha256": payload_sha256, "previous_sha256": previous_sha256, "task_content_evaluated": False}
            # A retained intent precedes replacement, so an interrupted write is
            # distinguishable from a completed tool return.
            intent = cluster.profile.base / (layout["prefix"] + "-artifact-write-" + uuid4().hex + ".json")
            save(intent, {**record, "state": "prepared"})
            replace_atomic(temporary, target)
            save(intent, {**record, "state": "completed"})
            return record
        finally:
            workbook.close()
            try: temporary.unlink(missing_ok=True)
            except OSError: pass  # Retain the original serialization/replacement error.


def read_report(root, cluster, *, task="meal"):
    layout = artifact_layout(task)
    root = Path(root).resolve()
    workspace = workspace_for(root, cluster, task=task)
    path = workspace / layout["filename"]
    no_links(path, root)
    with owned_lock(cluster.profile.base / ("." + layout["prefix"] + "-artifact.lock"), root, layout["lock_kind"]):
        if not path.is_file() or path.stat().st_size > MAX_BYTES: raise ValueError("Owned workbook missing or exceeds 1 MiB")
        raw = path.read_bytes()
        return {"artifact_ref": path.relative_to(root).as_posix(), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                "base64": base64.b64encode(raw).decode("ascii")}


def preserve_bytes(path, raw, root):
    no_links(path, root)
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw: raise ResourceError("Recovery archive bytes changed")
    else:
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())


def recover_artifact_lock(root, cluster, *, task="meal"):
    layout = artifact_layout(task)
    """Explicitly recover only a confirmed dead writer's exact artifact lock."""
    path = cluster.profile.base / ("." + layout["prefix"] + "-artifact.lock")
    no_links(path, root)
    if not path.exists(): return None
    if path.stat().st_size > 8192: raise ResourceError("Unexpected artifact lock size")
    raw = path.read_bytes()
    lock = json.loads(raw)
    if (lock.get("schema_version") != "workenv-lock-v0.1" or lock.get("kind") != layout["lock_kind"]
        or lock.get("workspace_owner") != str(root) or lock.get("resource") != path.relative_to(root).as_posix()):
        raise ResourceError("Artifact lock belongs to another resource")
    if process_identity(lock["process"]["pid"])["status"] not in ("not_found", "exited"):
        raise ResourceError("Artifact writer is alive or unknown; its lock was preserved")
    archive = cluster.profile.base / (layout["prefix"] + "-artifact-lock-history") / (hashlib.sha256(raw).hexdigest() + ".json")
    preserve_bytes(archive, raw, root)
    if path.read_bytes() != raw: raise ResourceError("Artifact lock changed during recovery")
    path.unlink()
    return archive.relative_to(root).as_posix()


def reconcile_write(root, cluster, intent_name, *, recover_stale_lock=False, task="meal"):
    layout = artifact_layout(task)
    """Resolve metadata from observed old/new bytes; never replay the workbook write."""
    root = Path(root).resolve()
    workspace = workspace_for(root, cluster, require_active=False, task=task)
    if not isinstance(intent_name, str) or not re.fullmatch(re.escape(layout["prefix"]) + r"-artifact-write-[0-9a-f]{32}\.json", intent_name):
        raise ValueError("Expected one owned artifact intent basename")
    recovered = recover_artifact_lock(root, cluster, task=task) if recover_stale_lock else None
    path = cluster.profile.base / intent_name
    no_links(path, root)
    target = workspace / layout["filename"]
    no_links(target, root)
    with owned_lock(cluster.profile.base / ("." + layout["prefix"] + "-artifact.lock"), root, layout["lock_kind"]):
        if path.stat().st_size > 8192: raise ResourceError("Unexpected artifact intent size")
        raw = path.read_bytes()
        record = json.loads(raw)
        digest = lambda value: isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
        if (not isinstance(record, dict) or "previous_sha256" not in record
            or record.get("schema_version") != layout["schema"] + "-artifact-write-v1" or record.get("profile") != cluster.profile.record()
            or record.get("artifact_ref") != target.relative_to(root).as_posix() or record.get("task_content_evaluated") is not False
            or type(record.get("bytes")) is not int or not 0 < record["bytes"] <= MAX_BYTES
            or not digest(record.get("sha256")) or not digest(record.get("payload_sha256"))
            or not (record.get("previous_sha256") is None or digest(record["previous_sha256"]))):
            raise ResourceError("Artifact intent identity or byte claims differ")
        if record.get("state") in ("completed", "aborted"):
            return {"schema_version": layout["schema"] + "-artifact-reconciliation-v1", "decision": "already_resolved", "state": record["state"],
                    "intent_ref": path.relative_to(root).as_posix(), "replay_performed": False, "recovered_lock_ref": recovered}
        if record.get("state") != "prepared": raise ResourceError("Unsupported artifact intent state")
        def current():
            no_links(target, root)
            if not target.exists(): return None, 0
            if not target.is_file() or target.stat().st_size > MAX_BYTES: raise ResourceError("Current artifact exceeds recovery budget")
            data = target.read_bytes()
            return hashlib.sha256(data).hexdigest(), len(data)
        observed, length = current()
        if observed == record["sha256"] and length == record["bytes"]:
            decision, state = "committed", "completed"
        elif observed == record["previous_sha256"]:
            decision, state = "not_committed", "aborted"
        else: raise ResourceError("Artifact bytes match neither old nor new write; retain unknown state")
        checksum = hashlib.sha256(raw).hexdigest()
        archive = cluster.profile.base / (layout["prefix"] + "-artifact-intent-history") / (checksum + ".json")
        preserve_bytes(archive, raw, root)
        if path.read_bytes() != raw or current() != (observed, length): raise ResourceError("Artifact or intent changed during reconciliation")
        resolution = {"schema_version": layout["schema"] + "-artifact-reconciliation-v1", "decision": decision, "state": state,
                      "intent_ref": path.relative_to(root).as_posix(), "original_intent_sha256": checksum,
                      "original_intent_ref": archive.relative_to(root).as_posix(), "observed_sha256": observed,
                      "replay_performed": False, "recovered_lock_ref": recovered}
        save(path, {**record, "state": state, "reconciliation": resolution})
        return resolution
