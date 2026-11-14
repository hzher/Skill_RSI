"""Audit generation transitions from immutable archives and actual HTTP journals."""
import hashlib
import re

from integrations.ledger_rotation import ledger_value, locations, retired_sessions
from integrations.resource_ledger import ResourceLedger
from integrations.mainline_audit import read_json, source_binding, track_input, track_absence
from workenv.core import contained

BACKENDS = {
    "synthetic": ("synthetic-sessions.json", "project_synthetic:diagnostic", "local-documents-11-base"),
    "isolated": ("isolated-sessions.json", "toolathlon_gym:nemo-isolated-diagnostic", "wc-sales-tax-summary"),
}
LEDGERS = ("resource-sessions.json", "synthetic-sessions.json", "isolated-sessions.json")


def rotation_backend(name="synthetic"):
    if not isinstance(name, str) or name not in BACKENDS:
        raise ValueError("Only fixed synthetic or isolated rotation diagnostics are supported")
    return BACKENDS[name]


def fresh_tools(backend, reply):
    rotation_backend(backend)
    if not isinstance(reply, dict): return False
    if backend == "synthetic":
        return reply.get("source") == "project_synthetic" and reply.get("training_eligible") is False
    tools, profile = reply.get("tools"), reply.get("task_profile", {})
    return (isinstance(profile, dict) and profile.get("task_id") == "wc-sales-tax-summary"
            and profile.get("resource_backend") == "isolated" and profile.get("training_admission") is False
            and isinstance(tools, list) and any(isinstance(t, dict) and t.get("server") == "woocommerce"
            and t.get("name") == "woo_orders_list" for t in tools))


def audit_rotation(root, receipt_path, *, backend=None):
    receipt = read_json(receipt_path, root)
    modern = receipt.get("schema_version") == "nemo-ledger-rotation-diagnostic-v2"
    selected = receipt.get("ledger") if modern else "synthetic"
    filename, taskset, task_id = rotation_backend(selected)
    if backend is not None and (rotation_backend(backend) != rotation_backend(selected)):
        raise ValueError("Rotation evidence belongs to another backend")
    required = {"owned_bridges_idle", "all_current_identities_closed", "plan_is_read_only", "owned_bridge_stopped",
        "closed_identities_retired_in_new_generation", "archive_bytes_unchanged", "same_nemo_process", "old_sid_cannot_reseed",
        "old_close_remains_idempotent", "old_closed_session_remains_unknown", "old_episode_binding_enforced",
        "fresh_session_works", "new_generation_contains_only_fresh_identity", "original_ledger_unchanged"}
    checks = receipt.get("checks", [])
    if (receipt.get("schema_version") not in ("nemo-ledger-rotation-diagnostic-v1", "nemo-ledger-rotation-diagnostic-v2") or receipt.get("passed") is not True
        or len(checks) != len(required) or {c.get("name") for c in checks} != required or any(c.get("passed") is not True for c in checks)):
        raise ValueError("Ledger rotation diagnostic coverage is incomplete")
    if receipt.get("execution_kind") != "scripted_resource_diagnostic" or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates")):
        raise ValueError("Ledger rotation cannot claim training or model work")
    source_binding(root, receipt.get("source_sha256"))
    if modern:
        expected_sources = {"scripts/verify_ledger_rotation.py", "integrations/rotation_audit.py",
                            "integrations/nemo_http.py", "integrations/runtime_identity.py"}
        if not expected_sources <= set(receipt.get("source_sha256", {})):
            raise ValueError("Rotation diagnostic implementation binding is incomplete")
        if any(receipt.get(k) is not False for k in ("business_task_executed", "official_benchmark_score_available")):
            raise ValueError("Resource rotation cannot claim business task or benchmark acceptance")
        others = receipt.get("other_ledgers")
        expected = set(LEDGERS) - {filename}
        if not isinstance(others, dict) or set(others) != expected:
            raise ValueError("Untargeted ledger snapshots are incomplete")
        for name, pair in others.items():
            if not isinstance(pair, dict) or set(pair) != {"before_sha256", "after_sha256"}:
                raise ValueError("Invalid untargeted ledger snapshot binding")
            raw = []
            for phase in ("before", "after"):
                snapshot = receipt_path.parent / (phase + "-" + name)
                read_json(snapshot, root)
                content = snapshot.read_bytes()
                ledger_value(content, root)
                if hashlib.sha256(content).hexdigest() != pair[phase + "_sha256"]:
                    raise ValueError("Untargeted ledger snapshot changed")
                raw.append(content)
            if raw[0] != raw[1]: raise ValueError("Another ledger was modified by rotation")
    journal = receipt_path.parent / "http_journal.json"
    events = read_json(journal, root, limit=4 * 1024 * 1024)
    if hashlib.sha256(journal.read_bytes()).hexdigest() != receipt.get("journal_sha256"):
        raise ValueError("Ledger rotation HTTP journal changed")
    if not isinstance(events, list) or not 1 <= len(events) <= 30:
        raise ValueError("Ledger rotation HTTP event budget exceeded")
    result = receipt.get("rotation") or {}
    if not re.fullmatch(r"[0-9a-f]{64}", result.get("expected_sha256", "")):
        raise ValueError("Invalid rotation archive checksum")
    path = root / ".runtime/nemo-vm" / filename
    _, base, _ = locations(path, root)
    reference = result.get("archive_ref", "")
    if (result.get("dry_run") is not False or result.get("state") != "completed" or result.get("ledger_ref") != path.relative_to(root).as_posix()
        or reference != (base / (result.get("expected_sha256", "") + ".json")).relative_to(root).as_posix()):
        raise ValueError("Unexpected ledger rotation archive/identity")
    archive = contained(root, reference)
    if archive.stat().st_size > 2 * 1024 * 1024: raise ValueError("Ledger archive exceeds its byte budget")
    raw = archive.read_bytes()
    track_input(archive, root, hashlib.sha256(raw).hexdigest())
    if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != result["expected_sha256"]:
        raise ValueError("Retained ledger bytes changed")
    rows = ledger_value(raw, root)
    retired = retired_sessions(path, root)
    index = path.with_name(path.stem + ".history.json")
    history = read_json(index, root)
    for entry in history["archives"]: read_json(base / (entry["sha256"] + ".json"), root)
    pending = base / "rotation-pending.json"
    if pending.exists(): read_json(pending, root)
    else: track_absence(pending, root)
    if len(rows) != result.get("closing_identities_retained") or any(retired.get(sid) != row for sid, row in rows.items()):
        raise ValueError("A closed identity was forgotten or rebound after rotation")
    prefix = "/" + selected
    if any(not isinstance(e, dict) or not isinstance(e.get("route"), str) or not e["route"].startswith(prefix + "/") for e in events):
        raise ValueError("HTTP journal contains another backend")
    seeded = [e for e in events if e.get("route") == prefix + "/seed_session" and e.get("status") == 200]
    if len(seeded) != 2: raise ValueError("Expected an old and a fresh resource session")
    old, fresh = [e["request"] for e in seeded]
    sid = old["resources_session_id"]
    expected_task = {"taskset": taskset, "task_id": task_id}
    if (old.get("task_id") != expected_task or fresh.get("task_id") != expected_task
            or old.get("task_data") != {} or fresh.get("task_data") != {}
            or fresh.get("episode_id") != old.get("episode_id")):
        raise ValueError("Rotation task/episode binding changed")
    if sid == fresh["resources_session_id"] or rows.get(sid, {}).get("binding") != old:
        raise ValueError("Archived identity does not match the original HTTP seed")
    rejected = [e for e in events if e.get("route") == prefix + "/seed_session" and e.get("status") == 409 and e.get("request") == old]
    closes = [e for e in events if e.get("route") == prefix + "/close_session" and e.get("request", {}).get("resources_session_id") == sid]
    if len(rejected) != 1 or sum(e.get("status") == 200 for e in closes) != 2 or sum(e.get("status") == 409 for e in closes) != 1:
        raise ValueError("Closed identity replay/episode guards were not observed")
    if any((e["request"].get("episode_id") == old["episode_id"]) != (e["status"] == 200) for e in closes):
        raise ValueError("Retired close did not preserve the episode binding")
    tools = [e for e in events if e.get("route") == prefix + "/tools" and e.get("status") == 200]
    if (len(tools) != 1 or tools[0]["request"].get("resources_session_id") != fresh["resources_session_id"]
        or tools[0]["request"].get("episode_id") != old["episode_id"] or not fresh_tools(selected, tools[0].get("response"))):
        raise ValueError("Fresh selected resource tools were not observed")
    verifies = [e for e in events if e.get("route") == prefix + "/verify" and e.get("status") == 200]
    if len(verifies) != 1: raise ValueError("Expected the old session's unknown measurement")
    event = verifies[0]
    request, reply = event["request"], event["response"]
    if (request.get("verification_input", {}).get("resources_session_id") != sid or reply.get("episode_id") != old["episode_id"]
        or request.get("episode_id") != old["episode_id"] or request.get("task_id") != old["task_id"] or reply.get("task_id") != old["task_id"]
        or reply.get("result", {}).get("reward") is not None or reply["result"].get("mask_sample") is not True):
        raise ValueError("Retired resource measurement is not bound, masked unknown")
    if modern:
        routes = [("/seed_session", 200), ("/close_session", 200), ("/seed_session", 409),
                  ("/close_session", 200), ("/verify", 200), ("/close_session", 409),
                  ("/seed_session", 200), ("/tools", 200)]
        if selected == "isolated": routes.append(("/tool", 200))
        routes.append(("/close_session", 200))
        if [(e["route"], e.get("status")) for e in events] != [(prefix+r, s) for r, s in routes]:
            raise ValueError("Rotation HTTP sequence is incomplete or has unreviewed operations")
        bound = {k: fresh[k] for k in ("resources_session_id", "episode_id")}
        if events[-1]["request"] != bound: raise ValueError("Fresh resource was not closed")
        read_json(path, root)
        current = ResourceLedger(path, root)
        fresh_row = current.closed_rows().get(fresh["resources_session_id"], {})
        if fresh_row.get("binding") != fresh:
            raise ValueError("Fresh resource closure is not retained in the owned ledger")
        if selected == "isolated":
            call = events[-2]
            request = call.get("request", {})
            if (set(request) != {*bound, "request_id", "server", "name", "arguments"}
                or any(request.get(k) != v for k, v in bound.items()) or request.get("server") != "woocommerce"
                or request.get("name") != "woo_orders_list" or request.get("arguments") != {"perPage": 1, "page": 1}
                or not isinstance(call.get("response", {}).get("result"), list) or not call["response"]["result"]):
                raise ValueError("Fresh isolated original read tool was not observed")
    return {"passed": True, "archived_closed_identities": len(rows), "currently_retired_identities": len(retired),
            "ledger": selected, "old_identity_reactivation_allowed": False,
            "business_task_executed": False, "receipt": receipt_path.relative_to(root).as_posix()}
