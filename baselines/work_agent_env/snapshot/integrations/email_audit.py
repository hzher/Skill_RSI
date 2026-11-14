"""Recompute retained original PG email effects without executing mail or tasks."""
import hashlib
from uuid import UUID

from adapters.email_pg_native import METHODS
from integrations.email_build import verify_build, source_inputs
from integrations.email_components import fingerprint, validate_arguments, verify_effect, RESET_TABLES, TABLES
from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_input, track_absence
from training.email_task_review import BASE, public_blob
from workenv.core import contained, ResourceError
from integrations import email_task_identity as identity


CHECKS = ("owned_build_and_empty_seed", "two_owned_pg_email_resources", "three_original_method_schemas",
    "explicit_pg_backends_and_forbidden_smtp_socket", "original_local_send_and_exact_sql_records", "same_request_no_duplicate_mail",
    "conflicting_request_id_refused", "unallowlisted_method_and_attachments_refused", "original_sent_list_and_read_mark",
    "unicode_mail_content_preserved", "resource_markers_cannot_be_rebound", "closing_first_keeps_second_active",
    "original_reset_isolated_and_readonly_unchanged", "partial_send_is_unknown_with_message_retained",
    "unknown_request_and_new_requests_never_replayed", "unknown_mail_resource_cannot_reset", "owned_email_children_closed",
    "owned_postgres_closed", "shared_database_and_nemo_ledgers_unchanged")
SOURCES = ("integrations/email_audit.py", "integrations/email_build.py", "integrations/email_components.py", "adapters/email_pg_native.py",
    "scripts/build_email_component.py", "scripts/prepare_email_component_seed.py", "scripts/verify_email_components.py",
    "training/email_task_review.py", "configs/email.requirements.in", "configs/email.requirements.lock.txt")
SOURCES += ("integrations/email_task_identity.py", "integrations/inventory_data.py", "training/inventory_task_review.py",
            "scripts/prepare_inventory_data_seed.py", "adapters/inventory_alert_validation.py", "adapters/inventory_alert_artifacts.py")

def audit_build(root, path):
    record = read_json(path, root)
    if (record.get("runtime_task_activated") is not False or record.get("original_sources_modified") is not False
        or any(type(record.get(k)) is not int or record[k] != 0 for k in ("external_messages_sent", "training_updates"))):
        raise ValueError("Email build cannot claim task execution or external mail")
    base, python = verify_build(root, record, check_environment=True)
    files, objects = source_inputs(root)
    if record["source_objects"] != objects or record["retained_sha256"] != {n: hashlib.sha256(b).hexdigest() for n,b in files.items()}:
        raise ValueError("Email build differs from pinned original Git objects")
    source_binding(root, record["source_sha256"])
    manifest = read_json(python.parent.parent / "environment_manifest.json", root, limit=8 * 1024 * 1024)
    owner = read_json(python.parent.parent.with_name(python.parent.parent.name + ".owner.json"), root)
    if owner != manifest["owner"] or owner["workspace"] != str(root.resolve()): raise ValueError("Email dependency owner changed")
    for name in ("build_owner.json", *files): track_input(base / name, root)
    return {"original_source_files": len(files), "installed_dependency_files_verified": len(manifest["files"]),
            "runtime_task_activated": False, "external_messages_sent": 0}


def audit_journal(root, profile, retained, *, task_id=identity.vip.TASK_ID):
    spec = identity.EmailTaskIdentity(task_id)
    run = profile.base / "email-components-run"
    journal = read_json(run / "requests.json", root, limit=32 * 1024 * 1024)
    if (journal.get("schema_version") != "owned-email-request-journal-v1" or journal.get("profile") != profile.record()
        or not isinstance(journal.get("entries"), dict) or not 1 <= len(journal["entries"]) <= 128):
        raise ValueError("Email request journal inventory differs")
    if (task_id != identity.vip.TASK_ID or "task_id" in journal or "sender" in journal) and (
        journal.get("task_id") != task_id or journal.get("sender") != spec.sender):
        raise ValueError("Email journal task or sender rebound")
    committed, unknown = [], []
    for request_id, entry in journal["entries"].items():
        if str(UUID(request_id)) != request_id: raise ValueError("Email journal identity differs")
        name, args = entry["name"], entry["arguments"]; validate_arguments(name, args)
        if entry["fingerprint"] != fingerprint(name, args): raise ValueError("Email request fingerprint differs")
        before_path = run / (request_id + ".before.json")
        if entry["before_ref"] != before_path.relative_to(root).as_posix() or entry["before_ref"] not in retained:
            raise ValueError("Email request snapshot was rebound")
        before = read_json(before_path, root, limit=8 * 1024 * 1024)
        after_path = run / (request_id + ".after.json")
        if entry.get("after_ref") != after_path.relative_to(root).as_posix() or entry["after_ref"] not in retained:
            raise ValueError("Email request after snapshot missing or rebound")
        after = read_json(after_path, root, limit=8 * 1024 * 1024)
        if entry["state"] == "committed":
            verify_effect(name, args, entry["response"], before, after, task_id=task_id); committed.append(entry)
        elif entry["state"] == "unknown":
            try: verify_effect(name, args, entry["response"], before, after, task_id=task_id)
            except ResourceError: pass
            else: raise ValueError("Email unknown fixture has no failed observation")
            if (name != "send_email" or entry.get("error_type") != "ResourceError" or before["office"] != after["office"]
                or len(after["email"]["messages"]) != len(before["email"]["messages"]) + 1
                or before["email"]["sent_log"] != after["email"]["sent_log"]):
                raise ValueError("Original email partial commit not retained")
            from copy import deepcopy
            complete = deepcopy(after)
            added = [r for r in after["email"]["messages"] if r["id"] not in {m["id"] for m in before["email"]["messages"]}]
            if len(added) != 1: raise ValueError("Partial mail message inventory differs")
            complete["email"]["sent_log"].append({"id": max([r["id"] for r in complete["email"]["sent_log"]] + [0]) + 1, "message_id": added[0]["id"]})
            verify_effect(name, args, {"content": [{"type": "text", "text": "Email sent successfully to " + args["to"]}]}, before, complete, task_id=task_id)
            unknown.append(entry)
        else: raise ValueError("Email diagnostic left an unobserved request")
    return committed, unknown


def audit_components(root, path):
    receipt = read_json(path, root)
    if (receipt.get("schema_version") != "owned-email-components-diagnostic-v1" or receipt.get("passed") is not True
        or [c["name"] for c in receipt["checks"]] != list(CHECKS) or any(c["passed"] is not True for c in receipt["checks"])
        or receipt.get("runtime_task_activated") is not False or receipt.get("task_passed") is not None or receipt.get("reward") is not None
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates", "training_admissions", "external_messages_sent"))
        or set(receipt.get("source_sha256", {})) != set(SOURCES)):
        raise ValueError("Email component receipt scope or checks differ")
    source_binding(root, receipt["source_sha256"])
    if receipt["build"] != read_json(root / "evidence/mainline/email_build.json", root): raise ValueError("Email component build rebound")
    retained = receipt.get("retained_sha256", {})
    if not 20 <= len(retained) <= 256: raise ValueError("Email retained inventory budget")
    profiles = [EpisodeProfile(root, r["resource_id"], r["slot"]) for r in receipt["profiles"]]
    if len(profiles) != 2 or [p.slot for p in profiles] != [0, 1] or profiles[0].resource_id == profiles[1].resource_id:
        raise ValueError("Email isolated profiles differ")
    for p, record in zip(profiles, receipt["profiles"]): p.validate_record(record)
    for name, checksum in retained.items():
        member = contained(root, name)
        if not any(member.is_relative_to(p.base) for p in profiles) and not member.is_relative_to(path.parent):
            raise ValueError("Email retained member outside this diagnostic")
        if member.stat().st_size > 8 * 1024 * 1024 or hashlib.sha256(member.read_bytes()).hexdigest() != checksum:
            raise ValueError("Email retained bytes differ")
        track_input(member, root, checksum)
    initial = read_json(path.parent / "initial_snapshots.json", root, limit=16 * 1024 * 1024)
    tasks = receipt.get("task_ids", [identity.vip.TASK_ID] * 2)
    if tasks not in ([identity.vip.TASK_ID] * 2, [identity.vip.TASK_ID, identity.inventory.TASK_ID]):
        raise ValueError("Email diagnostic task pair differs")
    if len(initial) != 2 or any(any(s["email"].values()) or s["office"].get("cells", []) or s["office"].get("events", []) for s in initial):
        raise ValueError("Email diagnostic did not start empty")
    counts, requests = [], []
    for i, profile in enumerate(profiles):
        run = profile.base / "email-components-run"
        observed = read_json(run / "email-process.json", root)
        schemas = read_json(run / "tool_schemas.json", root)
        state = read_json(run / "component_state.json", root)
        if (observed["profile"] != profile.record() or observed["methods"] != list(METHODS)
            or observed["forbidden_socket_blocked"] is not True or observed["external_messages_sent"] != 0
            or observed["build_fingerprint"] != receipt["build"]["build_fingerprint"]
            or observed["pg_data_ref"] != profile.data.relative_to(root).as_posix()
            or observed["backend_classes"] != ["emails_mcp.backends.pg_backend.PgIMAPBackend", "emails_mcp.backends.pg_backend.PgSMTPBackend"]
            or len(schemas) != 3 or {s["name"] for s in schemas} != set(METHODS)
            or state != {"profile": profile.record(), "outcome_unknown": bool(i), "nemo_task_executed": False,
                         "task_passed": None, "reward": None, "external_messages_sent": 0}):
            raise ValueError("Email original process, methods or closure outcome differs")
        spec = identity.EmailTaskIdentity(tasks[i])
        launch = read_json(run / "launch.json", root)
        if launch.get("schema_version") != "owned-email-task-launch-v1" or launch.get("build") != receipt["build"]:
            raise ValueError("Email retained launch build differs")
        binding = observed.get("task_binding", {})
        if (binding != launch.get("task_binding") or binding.get("task_id") != spec.task_id or binding.get("sender") != spec.sender
            or binding.get("profile") != profile.record() or launch.get("profile") != profile.record()
            or binding.get("build_sha256") != identity._build_digest(receipt["build"])):
            raise ValueError("Email task process binding differs")
        committed, unknown = audit_journal(root, profile, retained, task_id=spec.task_id)
        counts.append((len(committed), len(unknown))); requests.extend(committed)
        require_dependents_closed(profile); track_absence(profile.data / "postmaster.pid", root)
        dependents = read_json(profile.base / "dependent_processes.json", root)
        if dependents["children"]["email"]["before"]["pid"] != observed["pid"]: raise ValueError("Email closure PID differs")
        if observed.get("schema_version") != "owned-email-task-process-v1" or observed.get("process") != dependents["children"]["email"]["before"]:
            raise ValueError("Email retained child birth identity differs")
        expected = identity._preprocess(root, spec)
        marker = read_json(profile.base / spec.marker, root)
        if marker["profile"] != profile.record() or marker["task_id"] != spec.task_id or marker["preprocess_source"] != expected: raise ValueError("Email reset source rebound")
        original_marker = read_json(run / "initialization.json", root)
        if (hashlib.sha256((run / "initialization.json").read_bytes()).hexdigest() != binding.get("marker_sha256")
            or original_marker.get("state") != "ready" or original_marker.get("task_id") != spec.task_id
            or original_marker.get("profile") != profile.record() or original_marker.get("preprocess_source") != expected):
            raise ValueError("Email retained launch marker differs")
        # Slot zero was intentionally reset after child exit.
        if i == 1 and binding.get("marker_sha256") != hashlib.sha256((profile.base / spec.marker).read_bytes()).hexdigest():
            raise ValueError("Email launch initialization bytes differ")
        if binding.get("preprocess_source") != expected or binding.get("readonly_sha256") != initial[i]["office"]["readonly_sha256"]:
            raise ValueError("Email launch preprocess or read-only source differs")
        prefix = "email" if spec.task_id == identity.vip.TASK_ID else "inventory"
        preprocess = list(profile.base.glob(prefix + "-preprocess-*/original_preprocess.py"))
        if len(preprocess) != (2 if i == 0 else 1) or any(hashlib.sha256(p.read_bytes()).hexdigest() != expected["sha256"] for p in preprocess):
            raise ValueError("Email reset count or original script differs")
    if counts != [(4, 0), (2, 1)]: raise ValueError("Email request/replay inventory differs")
    reset = read_json(path.parent / "reset_snapshots.json", root, limit=16 * 1024 * 1024)
    if any(reset[0]["email"][n] for n in RESET_TABLES) or reset[1] != read_json(contained(root, unknown[0]["before_ref"]), root):
        raise ValueError("Email isolated reset differs")
    if read_json(path.parent / "partial_write_snapshot.json", root) != read_json(contained(root, unknown[0]["after_ref"]), root):
        raise ValueError("Partial mail observation changed after blocked replays")
    if not any("\u611f\u8c22" in e["arguments"].get("body", "") for e in requests): raise ValueError("Unicode email fixture missing")
    if receipt["shared_before"] != receipt["shared_after"]: raise ValueError("Shared database changed during email diagnostic")
    ledgers = receipt.get("ledger_sha256_before", {})
    if set(ledgers) != {"resource-sessions.json", "isolated-sessions.json", "synthetic-sessions.json"} or ledgers != receipt.get("ledger_sha256_after"):
        raise ValueError("Email diagnostic changed NeMo ledgers")
    for name, checksum in ledgers.items():
        for phase in ("before", "after"):
            member = path.parent / ("ledger-" + phase + "-" + name)
            if member.relative_to(root).as_posix() not in retained or hashlib.sha256(member.read_bytes()).hexdigest() != checksum:
                raise ValueError("Retained NeMo ledger bytes differ")
    return {"checks": len(CHECKS), "committed_requests_recomputed": 6, "partial_writes_recomputed": 1,
            "task_ids": tasks, "senders": [identity.EmailTaskIdentity(t).sender for t in tasks],
            "runtime_profiles_added": 0, "external_messages_sent": 0, "reward": None}
