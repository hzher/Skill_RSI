"""Byte-bound audit of synthetic diagnostics, separate from seed/evaluation scores."""
import hashlib
import json
import re
from collections import Counter

from integrations.mainline_audit import read_json, source_binding, track_input, reject_nonfinite
from workenv.core import contained, no_links


def audit_incremental(root, receipt_path, receipt, events, expected_tasks):
    from integrations.diagnostic_recorder import MAX_JOURNAL_BYTES, MAX_EVENT_BYTES, MAX_EVENTS
    directory = receipt_path.parent
    progress_path, journal_path = directory / "progress.json", directory / "http_journal.partial.jsonl"
    progress = read_json(progress_path, root)
    if hashlib.sha256(progress_path.read_bytes()).hexdigest() != receipt.get("progress_sha256"):
        raise ValueError("Incremental diagnostic progress changed")
    no_links(journal_path, root)
    if journal_path.stat().st_size > MAX_JOURNAL_BYTES: raise ValueError("Incremental journal exceeds its byte budget")
    raw = journal_path.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    track_input(journal_path, root, checksum)
    if checksum != receipt.get("incremental_journal_sha256") or checksum != progress.get("journal_prefix_sha256"):
        raise ValueError("Incremental journal hash differs from its progress or final receipt")
    lines = raw.splitlines()
    if not raw.endswith(b"\n") or not 1 <= len(lines) <= MAX_EVENTS or any(len(line) > MAX_EVENT_BYTES for line in lines):
        raise ValueError("Incremental journal is incomplete or exceeds its event budget")
    incremental = []
    for index, line in enumerate(lines):
        event = json.loads(line, parse_constant=reject_nonfinite)
        if type(event.get("event_index")) is not int or event.pop("event_index") != index:
            raise ValueError("Incremental journal sequence is invalid")
        incremental.append(event)
    canonical = lambda event: json.dumps(event, sort_keys=True, allow_nan=False, separators=(",", ":"))
    if Counter(map(canonical, incremental)) != Counter(map(canonical, events)):
        raise ValueError("Incremental events differ from the final HTTP journal")
    if (progress.get("schema_version") != "scripted-diagnostic-progress-v1" or progress.get("state") != "passed"
        or progress.get("passed") is not True or progress.get("expected_tasks") != list(expected_tasks)
        or progress.get("completed_tasks") != list(expected_tasks) or progress.get("in_flight_task") is not None
        or progress.get("event_count") != len(lines) or progress.get("journal_bytes") != len(raw)
        or progress.get("automatic_resume") is not False or progress.get("training_eligible") is not False
        or progress.get("source_sha256") != receipt["source_sha256"]):
        raise ValueError("Incremental progress does not prove complete diagnostic coverage")
    return {"event_count": len(lines), "completed_tasks": len(expected_tasks), "partial_execution_accepted": False}


def required_checks(task_ids):
    suffixes = ("idempotent_seed", "synthetic_source_only", "measured_failure", "measured_success", "closed_session_unknown")
    return {"four_registered_synthetic_families", "cookie_source_and_task_isolation"} | {
        task_id + ":" + suffix for task_id in task_ids for suffix in suffixes}


def audit_synthetic(root, receipt_path):
    receipt = read_json(receipt_path, root)
    if receipt.get("schema_version") != "nemo-synthetic-diagnostic-v1" or receipt.get("passed") is not True:
        raise ValueError("Synthetic diagnostics did not pass")
    if receipt.get("source") != "project_synthetic" or receipt.get("training_eligible") is not False:
        raise ValueError("Synthetic source/training boundary changed")
    for key in ("real_model_calls", "training_updates", "admitted_training_samples"):
        if type(receipt.get(key)) is not int or receipt[key] != 0:
            raise ValueError("Synthetic diagnostic counters cannot claim training or model samples")
    source_binding(root, receipt.get("source_sha256"))
    if "tasks/registry.json" not in receipt["source_sha256"]:
        raise ValueError("Diagnostic registry was not bound")
    specs = read_json(root / "tasks/registry.json", root)["tasks"]
    expected = {t["task_id"]: t for t in specs if t.get("source") == "project_synthetic" and t.get("runtime_adapter") in ("local_files", "local_tickets")}
    rows = receipt.get("tasks", [])
    if not expected or len(rows) != len(expected) or {r["task_id"] for r in rows} != set(expected) or receipt.get("task_count") != len(expected) or receipt.get("expected_task_count") != len(expected):
        raise ValueError("Synthetic task coverage changed or duplicated")
    checks = receipt.get("checks", [])
    names = required_checks(expected)
    if len(checks) != len(names) or {c.get("name") for c in checks} != names or any(c.get("passed") is not True for c in checks):
        raise ValueError("Synthetic required checks were not all completed")
    path = receipt_path.parent / "http_journal.json"
    events = read_json(path, root, limit=16 * 1024 * 1024)
    if hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get("http_journal_sha256"):
        raise ValueError("Synthetic HTTP journal changed")
    if not isinstance(events, list) or not 1 <= len(events) <= 4000:
        raise ValueError("Synthetic HTTP event budget exceeded")
    audit_incremental(root, receipt_path, receipt, events, expected)
    observed = {name: {"references": {}, "unknown": 0} for name in expected}
    bindings = {}
    for event in events:
        if event.get("route") == "/synthetic/seed_session" and event.get("status") == 200:
            body = event["request"]
            sid = body["resources_session_id"]
            binding = (body["episode_id"], body["task_id"])
            if sid in bindings and bindings[sid] != binding:
                raise ValueError("Synthetic seed identity was rebound")
            bindings[sid] = binding
        if event.get("route") != "/synthetic/verify" or event.get("status") != 200:
            continue
        request, reply = event["request"], event["response"]
        task = request["task_id"]
        if bindings.get(request["verification_input"]["resources_session_id"]) != (request["episode_id"], task):
            raise ValueError("Synthetic verification does not match its seeded resource")
        if task.get("taskset") != "project_synthetic:diagnostic" or task["task_id"] not in expected or reply.get("task_id") != task or reply.get("episode_id") != request["episode_id"]:
            raise ValueError("Synthetic reply task/episode mismatch")
        measurement = reply["result"]
        if measurement.get("source") != "project_synthetic" or measurement.get("training_eligible") is not False:
            raise ValueError("Synthetic measurement leaked into a training source")
        row = observed[task["task_id"]]
        if measurement.get("mask_sample") is True:
            if measurement.get("reward") is not None:
                raise ValueError("Unknown synthetic measurement became numeric")
            row["unknown"] += 1
        elif measurement.get("mask_sample") is False:
            reward = measurement.get("reward")
            reference = measurement.get("evaluation_ref", "")
            if type(reward) not in (int, float) or reward not in (0, 1) or not re.fullmatch(r"runs/nemo_synthetic_[0-9a-f]{32}/nemo_verify_[0-9a-f]{32}\.json", reference):
                raise ValueError("Synthetic reward or evaluator reference is invalid")
            original = read_json(contained(root, reference), root)
            run = read_json(contained(root, reference).parent / "run.json", root)
            if run.get("task_id") != task["task_id"] or run.get("workspace_owner") != str(root):
                raise ValueError("Retained synthetic evaluator belongs to another run/task")
            if original != measurement.get("evaluation") or original.get("evaluation_status") != "completed" or type(original.get("task_passed")) is not bool or float(original["task_passed"]) != reward:
                raise ValueError("Synthetic reward disagrees with retained evaluation")
            if reward in row["references"]:
                raise ValueError("Synthetic measurement duplicated")
            row["references"][reward] = reference
        else:
            raise ValueError("Synthetic measurement validity missing")
    for item in rows:
        actual = observed[item["task_id"]]
        if (item.get("source") != "project_synthetic" or item.get("family") != expected[item["task_id"]]["family"] or
            actual["references"] != {0: item["negative_evaluation_ref"], 1: item["positive_evaluation_ref"]} or actual["unknown"] != 1):
            raise ValueError("Synthetic measurements do not cover failure, success and closed unknown")
    return {"passed": True, "task_count": len(expected), "valid_measurements": 2 * len(expected),
            "unknown_measurements": len(expected), "source": "project_synthetic", "training_eligible": False,
            "receipt": receipt_path.relative_to(root).as_posix()}
