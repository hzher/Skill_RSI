"""Exercise original large UTF-8 MCP results through the real NeMo HTTP path."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

from mainline_runtime import ROOT, VM, bridge_request
sys.path.insert(0, str(ROOT))
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile, require_dependents_closed
from integrations.meal_plan_services import METHODS, MAX_ARGUMENT_BYTES
from integrations.nemo_http import NeMoHTTP
from integrations.resource_transport import MAX_RESPONSE_FRAME_BYTES, MAX_TOOL_RESPONSE_BYTES, wire_json
from integrations.response_budget_audit import CHECKS, DESCRIPTION, LIST_ARGUMENTS, EXTRA_SOURCES, MAX_EVIDENCE_BYTES, SCHEMA, created_id, listed_events
from integrations.runtime_identity import expected_identity
from integrations.task_routing import TaskRouter
from scripts.verify_calendar_isolation import query, shared_snapshot
from workenv.core import save
from workenv.process_identity import process_identity


def main():
    output = ROOT / "evidence/mainline" / ("nemo-response-budget-" + uuid4().hex)
    output.mkdir()
    client = NeMoHTTP()
    seed = {"resources_session_id": str(uuid4()),
        "episode_id": {"rollout_id": "nemo-response-budget-" + uuid4().hex, "attempt": 0},
        "task_id": {"taskset": "toolathlon_gym:nemo-response-budget", "task_id": "howtocook-meal-plan-gcal"}, "task_data": {}}
    bound = {key: seed[key] for key in ("resources_session_id", "episode_id")}
    checks, retained, observations, cleanup_errors = [], {}, {}, []
    attempted, closed, profile = False, False, None
    before, after = None, None
    def retain(path):
        if path.stat().st_size > MAX_EVIDENCE_BYTES:
            raise ValueError("Response diagnostic evidence exceeds its budget")
        with path.open("rb") as stream:
            retained[path.relative_to(ROOT).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    def check(name, valid):
        checks.append({"name": name, "passed": bool(valid)})
        if not valid:
            raise AssertionError(name)
    def post(route, body, expected=200):
        return client.post("/isolated" + route, body, expected=expected)
    def tool_body(name, arguments):
        return {**bound, "request_id": str(uuid4()), "server": "google_calendar", "name": name, "arguments": arguments}
    def event(index):
        return {"summary": "UTF-8 response diagnostic " + str(index), "description": DESCRIPTION,
            "start": {"dateTime": "2026-03-09T07:00:00-04:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-03-09T08:00:00-04:00", "timeZone": "America/New_York"}}
    def sql_rows():
        return [list(row) for row in query(EpisodeCluster(profile),
            "SELECT id,summary,octet_length(description),md5(description) FROM gcal.events ORDER BY id")]
    def other_ledgers():
        return {name: hashlib.sha256((VM / name).read_bytes()).hexdigest()
                for name in ("resource-sessions.json", "synthetic-sessions.json")}
    try:
        before = shared_snapshot()
        observations["other_ledgers_before"] = other_ledgers()
        if bridge_request("/status")["isolated"]["active_sessions"]:
            raise RuntimeError("Response-budget probe requires an idle isolated runtime")
        attempted = True
        post("/seed_session", seed)
        base = ROOT / ".runtime/native-episodes" / seed["resources_session_id"]
        owner = json.loads((base / "cluster_owner.json").read_text(encoding="utf-8"))
        profile = EpisodeProfile(ROOT, seed["resources_session_id"], owner["slot"])
        profile.validate_record(owner)
        lease = json.loads((base.parent / ("slot-" + str(profile.slot) + ".lock")).read_text(encoding="utf-8"))
        observations["worker_before"] = lease["process"]
        if process_identity(lease["process"]["pid"]) != lease["process"] or lease["process"]["status"] != "alive":
            raise RuntimeError("Response-budget worker process identity is not confirmed")
        schema = post("/tools", bound)
        observations["initial_calendar_rows"] = sql_rows()
        check(CHECKS[0], observations["initial_calendar_rows"] == []
            and schema["task_profile"] == TaskRouter(ROOT, "isolated").authorize(seed)
            and {(t["server"], t["name"]) for t in schema["tools"]} == {(s, n) for s, names in METHODS.items() for n in names})
        ids = []
        for index in range(16):
            args = event(index)
            if len(wire_json(args)) > MAX_ARGUMENT_BYTES:
                raise AssertionError("Individual event exceeds its original service argument budget")
            ids.append(created_id(post("/tool", tool_body("create_event", args))["result"]))
        observations["rows_after_sixteen"] = sql_rows()
        check(CHECKS[1], len(ids) == len(set(ids)) == len(observations["rows_after_sixteen"]) == 16)
        listing = tool_body("list_events", dict(LIST_ARGUMENTS))
        first = post("/tool", listing)
        events = listed_events(first["result"])
        size = len(wire_json(first["result"]))
        check(CHECKS[2], 3 * 1024 * 1024 < size <= MAX_TOOL_RESPONSE_BYTES
            and len(wire_json(first)) <= MAX_RESPONSE_FRAME_BYTES and len(events) == 16
            and {e["id"] for e in events} == set(ids) and all(e["description"] == DESCRIPTION for e in events))
        repeated = post("/tool", listing)
        check(CHECKS[3], repeated == first)
        for index in range(16, 20):
            ids.append(created_id(post("/tool", tool_body("create_event", event(index)))["result"]))
        observations["rows_after_twenty"] = sql_rows()
        check(CHECKS[4], len(set(ids)) == len(observations["rows_after_twenty"]) == 20
            and sum(row[2] for row in observations["rows_after_twenty"]) > MAX_TOOL_RESPONSE_BYTES)
        oversized = tool_body("list_events", dict(LIST_ARGUMENTS))
        post("/tool", oversized, expected=503)
        check(CHECKS[5], True)
        post("/tool", oversized, expected=409)
        check(CHECKS[6], True)
        post("/tool", tool_body("create_event", {**event(20), "summary": "must-not-execute", "description": "blocked-after-unknown"}), expected=503)
        check(CHECKS[7], True)
        measurement = post("/verify", {"episode_id": seed["episode_id"], "task_id": seed["task_id"],
            "verification_input": {"resources_session_id": seed["resources_session_id"]}})["result"]
        check(CHECKS[8], measurement["reward"] is None and measurement["mask_sample"] is True)
        observations["rows_after_rejections"] = sql_rows()
        check(CHECKS[9], observations["rows_after_rejections"] == observations["rows_after_twenty"])
        post("/close_session", bound)
        closed = True
        require_dependents_closed(profile)
        observations["worker_after"] = process_identity(observations["worker_before"]["pid"])
        observations["gateway_closed_row"] = json.loads((VM / "isolated-sessions.json").read_text(encoding="utf-8"))["sessions"][profile.resource_id]
        services = json.loads((base / "meal-task-session/services-closed.json").read_text(encoding="utf-8"))
        check(CHECKS[10], not (profile.data / "postmaster.pid").exists()
            and profile.resource_id not in bridge_request("/status")["isolated"]["active_sessions"]
            and observations["gateway_closed_row"]["state"] == "closed"
            and observations["worker_after"]["status"] in ("not_found", "exited")
            and services["outcome_unknown"] is True and services["calls"] == 22)
        after = shared_snapshot()
        observations["other_ledgers_after"] = other_ledgers()
        check(CHECKS[11], before == after and observations["other_ledgers_before"] == observations["other_ledgers_after"])
    finally:
        if attempted and not closed:
            try:
                post("/close_session", bound)
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__ + ": " + str(exc)[:300])
        if profile is not None:
            for name in ("cluster_owner.json", "resources-session.json", "dependent_processes.json",
                "meal-task-session/task_profile.json", "meal-task-session/nemo_binding.json", "meal-task-session/initial_state.json",
                "meal-task-session/run.json", "meal-task-session/nemo_closed.json", "meal-task-session/services-closed.json", "meal-task-session/trace.json"):
                path = profile.base / name
                if path.is_file():
                    retain(path)
        save(output / "observations.json", observations)
        retain(output / "observations.json")
        journal = wire_json(client.journal)
        if len(journal) > MAX_EVIDENCE_BYTES:
            raise ValueError("Response-budget journal exceeds its retained byte budget")
        (output / "http_journal.json").write_bytes(journal + b"\n")
        retain(output / "http_journal.json")
        result = {"schema_version": SCHEMA, "at": datetime.now(timezone.utc).isoformat(), "framework": "nemo_gym",
            "passed": [c["name"] for c in checks] == list(CHECKS) and all(c["passed"] for c in checks) and not cleanup_errors,
            "checks": checks, "seed": seed, "profile": profile.record() if profile else None,
            "cleanup_errors": cleanup_errors, "retained_sha256": retained,
            "journal_ref": (output / "http_journal.json").relative_to(ROOT).as_posix(),
            "observations_ref": (output / "observations.json").relative_to(ROOT).as_posix(),
            "shared_before": before, "shared_after": after, "training_eligible": False,
            "training_admissions": 0, "real_model_calls": 0, "training_updates": 0,
            "reference_solutions_read": False, "verified_task_bodies_read": False,
            "source_sha256": {**expected_identity(ROOT), **{name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in EXTRA_SOURCES}}}
        save(output / "result.json", result)
        print(json.dumps({"passed": result["passed"], "checks": len(checks), "receipt": (output / "result.json").relative_to(ROOT).as_posix()}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
