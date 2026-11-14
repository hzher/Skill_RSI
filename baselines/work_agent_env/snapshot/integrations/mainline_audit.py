"""Read-only consistency checks for the real NeMo diagnostic evidence."""
import hashlib
import json
from pathlib import Path
import re
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar

from workenv.core import contained, no_links
_READS = ContextVar("mainline_audit_read_inputs", default=None)


@contextmanager
def collect_inputs(root, *, archive_ref=None, selection_draft=False):
    if type(selection_draft) is not bool or selection_draft and archive_ref is not None:
        raise ValueError("Selection drafts cannot reuse an archive authority")
    state = {"root": root.resolve(), "files": {}, "missing": set(), "seen": {}, "archived": set()}
    if selection_draft:
        state.update(selection_draft=True, draft_members=set(), draft_direct=set(), draft_sizes={}, draft_bytes=0)
    if archive_ref is not None:
        from integrations.archive_inputs import load
        state["archive"] = load(root, archive_ref)
        state["files"][archive_ref] = state["archive"]["sha256"]
    token = _READS.set(state)
    try: yield state
    finally: _READS.reset(token)


def track_input(path, root, checksum=None):
    state = _READS.get()
    if state is None: return
    no_links(path, root)
    if root.resolve() != state["root"]: raise ValueError("Audit input belongs to another workspace")
    name = path.relative_to(root).as_posix()
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024: raise ValueError("Audit input exceeds its byte budget")
    if checksum is None:
        if path.stat().st_size > 32 * 1024 * 1024: raise ValueError("Audit input exceeds its byte budget")
        with path.open("rb") as stream: checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    previous = state["seen"].get(name)
    if previous is not None and previous != checksum: raise ValueError("Audit input changed during collection: " + name)
    state["seen"][name] = checksum
    member = state.get("archive", {}).get("members", {}).get(name)
    if member is not None:
        if checksum != member["sha256"]: raise ValueError("Archived original changed during collection: " + name)
        state["archived"].add(name)
    else: state["files"][name] = checksum
    if state.get("selection_draft"):
        from integrations import evidence_archive as ea
        try: ea.evidence_name(name)
        except ValueError: group = state["draft_direct"]
        else: group = state["draft_members"]
        group.add(name)
        size = path.stat().st_size
        state["draft_bytes"] += size - state["draft_sizes"].get(name, 0)
        state["draft_sizes"][name] = size
        if (len(state["draft_members"]) > 4096 or len(state["draft_direct"]) + 1 > 4096
            or len(state["files"]) > 8191 or state["draft_bytes"] > ea.MAX_CONTENT):
            raise ValueError("Selection draft exceeds bounded member/authority/content capacity")
    elif len(state["files"]) > 4096: raise ValueError("Audit input inventory exceeds its budget")


def track_absence(path, root):
    no_links(path, root)
    if path.exists(): raise ValueError("Expected stopped-resource marker is present: " + path.relative_to(root).as_posix())
    state = _READS.get()
    if state is not None: state["missing"].add(path.relative_to(root).as_posix())


def inventory_snapshot(root):
    from workenv.source_inventory import source_files
    program = []
    for path in source_files(root, include_mainline=True):
        name = path.relative_to(root).as_posix()
        if name.startswith(("docs/", "reports/")) or name in ("README.md", ".gitignore"): continue
        program.append(name)
    receipts = sorted(p.relative_to(root).as_posix() for p in (root / "evidence/mainline").glob("*/result.json"))
    if len(program) > 4096 or len(receipts) > 4096: raise ValueError("Audit path inventory exceeds its budget")
    return {"program_files": sorted(program), "result_receipts": receipts}


def inputs_current(root, receipt):
    if receipt.get("schema_version") == "evidence-selection-draft-v1" or "selection_draft" in receipt:
        return False
    hashes, missing = receipt.get("input_sha256"), receipt.get("expected_absent", [])
    if not isinstance(hashes, dict) or not 1 <= len(hashes) <= 4096 or not isinstance(missing, list) or len(missing) > 4096:
        return False
    total = 0
    try:
        if "archive_inputs" in receipt:
            from integrations.archive_inputs import current
            if not current(root, receipt): return False
        else:
            if ("input_budget" in receipt or "logical_input_manifest_sha256" in receipt
                or any(isinstance(n, str) and n.startswith(".runtime/evidence-archives/") and n.endswith(".zip") for n in hashes)):
                return False
            for name, checksum in hashes.items():
                if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum): return False
                path = contained(root, name)
                total += path.stat().st_size
                if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024 or total > 128 * 1024 * 1024: return False
                with path.open("rb") as stream: actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != checksum: return False
        if not all(not contained(root, name).exists() for name in missing): return False
        return "input_inventory" not in receipt or inventory_snapshot(root) == receipt["input_inventory"]
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile): return False

SCHEMAS = {
    "nemo-toolathlon-diagnostic-v1": {"idempotent_seed", "cookie_isolation", "real_original_mcp_schemas",
        "measured_business_failure_not_masked", "qualifying_orders", "artifact_written_through_nemo",
        "mutation_request_replay_returns_original_result", "original_and_supplemental_verifiers_pass",
        "idempotent_close", "late_seed_cannot_recreate_closed_session", "lost_session_is_masked_unknown"},
    "nemo-resource-recovery-diagnostic-v1": {"owned_session_before_fault", "exact_owned_process_identity",
        "native_lock_retained_after_fault", "orphan_session_reset_and_durably_closed", "late_seed_refused_after_process_restart",
        "orphan_has_unknown_masked_reward", "fresh_session_after_recovery"},
    "nemo-resource-expiry-diagnostic-v1": {"short_lease_acquired", "expired_session_is_unknown_masked",
        "expired_state_durably_closed_and_task_released", "expired_identity_cannot_be_recreated", "expired_close_is_idempotent"},
}


def reject_nonfinite(_):
    raise ValueError("Nonfinite evidence value")


def read_json(path: Path, root: Path, *, limit=2 * 1024 * 1024):
    no_links(path, root)
    if not path.is_file() or path.stat().st_size > limit: raise ValueError("Missing or oversized evidence entry")
    raw = path.read_bytes()
    track_input(path, root, hashlib.sha256(raw).hexdigest())
    return json.loads(raw.decode("utf-8"), parse_constant=reject_nonfinite)


def source_binding(root: Path, hashes: dict):
    if not isinstance(hashes, dict) or not hashes: raise ValueError("Missing source-byte binding")
    for name, expected in hashes.items():
        if not isinstance(name, str) or (name != "tasks/registry.json" and name.split("/", 1)[0] not in {"integrations", "training", "evaluation", "scripts", "configs", "adapters", "tests", "workenv", "resources_servers", "responses_api_agents"}):
            raise ValueError("Source reference is outside the code allowlist")
        path = contained(root, name)
        no_links(path, root)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Invalid source hash or oversized source")
        with path.open("rb") as source: actual = hashlib.file_digest(source, "sha256").hexdigest()
        track_input(path, root, actual)
        if actual != expected: raise ValueError("Source changed after the recorded check: " + name)
    return True


def initial_state_binding(root: Path, run: Path):
    metadata = read_json(run / "run.json", root)
    if metadata.get("initial_state_ref") != "initial_state.json":
        raise ValueError("Unexpected original resource initial-state reference")
    path = run / "initial_state.json"
    initial = read_json(path, root)
    if hashlib.sha256(path.read_bytes()).hexdigest() != metadata.get("initial_state_sha256"):
        raise ValueError("Original resource initial-state bytes changed")
    if not isinstance(initial, dict) or not isinstance(initial.get("tables"), dict) or not isinstance(initial.get("files"), dict):
        raise ValueError("Original resource initial-state schema is invalid")
    return metadata


def audit_diagnostic(root: Path, receipt_path: Path):
    receipt = read_json(receipt_path, root)
    expected_checks = SCHEMAS.get(receipt.get("schema_version"))
    checks = receipt.get("checks", [])
    if (not expected_checks or receipt.get("passed") is not True or len(checks) != len(expected_checks)
        or {c.get("name") for c in checks} != expected_checks or any(c.get("passed") is not True for c in checks)):
        raise ValueError("Diagnostic did not complete all of its required checks")
    if any(type(receipt.get(key)) is not int or receipt[key] != 0 for key in ("real_model_calls", "training_updates")):
        raise ValueError("Scripted diagnostic counters cannot claim model calls or training updates")
    source_binding(root, receipt.get("source_sha256"))
    journal_path = receipt_path.parent / "http_journal.json"
    if not isinstance(receipt.get("journal_sha256"), str): raise ValueError("HTTP journal was not bound at collection time")
    no_links(journal_path, root)
    if journal_path.stat().st_size > 16 * 1024 * 1024: raise ValueError("HTTP evidence budget exceeded")
    raw = journal_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != receipt["journal_sha256"]: raise ValueError("HTTP journal changed after receipt")
    events = json.loads(raw)
    if not isinstance(events, list) or not 1 <= len(events) <= 200: raise ValueError("Invalid HTTP event inventory")
    valid, unknown = 0, 0
    for event in events:
        if event.get("route") != "/verify" or event.get("status") != 200: continue
        reply, request = event["response"], event["request"]
        if reply.get("episode_id") != request.get("episode_id") or reply.get("task_id") != request.get("task_id"):
            raise ValueError("Verification reply belongs to a different task/episode")
        measurement = reply["result"]
        reward, masked = measurement.get("reward"), measurement.get("mask_sample")
        if masked is True:
            if reward is not None: raise ValueError("Unknown resource measurement was converted to a numeric task reward")
            unknown += 1
        elif masked is False:
            if type(reward) not in (int, float) or reward not in (0, 1): raise ValueError("Invalid measured task reward")
            reference = measurement.get("evaluation_ref", "")
            if not reference.startswith("runs/") or not reference.endswith("/evaluation.json"):
                raise ValueError("Valid measurement is missing its original evaluator record")
            original = read_json(contained(root, reference), root)
            run = contained(root, reference).parent.parent
            initial_state_binding(root, run)
            seeded = read_json(run / "nemo_binding.json", root)
            if seeded.get("episode_id") != request["episode_id"] or seeded.get("task_id") != request["task_id"]:
                raise ValueError("Original evaluation does not match its seeded task/episode")
            if original != measurement.get("evaluation") or original.get("evaluation_status") != "completed" or type(original.get("task_passed")) is not bool or float(original["task_passed"]) != reward:
                raise ValueError("Mapped reward disagrees with the retained original task evaluation")
            valid += 1
        else:
            raise ValueError("Verification did not explicitly classify measurement validity")
    return {"receipt": receipt_path.relative_to(root).as_posix(), "valid_measurements": valid, "unknown_measurements": unknown,
            "journal_sha256": receipt["journal_sha256"], "passed": True}
