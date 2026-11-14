"""Real NeMo second-original-task success/failure/unknown and two-episode check."""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

from mainline_runtime import ROOT, VM, bridge_request
sys.path.insert(0, str(ROOT))
from diagnostic_meal_plan import public_plan
from integrations.calendar_component import require_schema
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile, require_dependents_closed
from integrations.meal_plan_services import METHODS, components
from integrations.nemo_http import NeMoHTTP
from integrations.nemo_meal_audit import CHECKS, EXTRA_SOURCES
from integrations.runtime_identity import expected_identity
from integrations.task_routing import TaskRouter
from scripts.verify_calendar_isolation import shared_snapshot
from workenv.core import no_links, save


def main():
    output = ROOT / "evidence/mainline" / ("nemo-meal-plan-" + uuid4().hex)
    output.mkdir()
    clients = [NeMoHTTP() for _ in range(3)]
    seeds = [{"resources_session_id": str(uuid4()), "episode_id": {"rollout_id": "nemo-meal-" + uuid4().hex, "attempt": 0},
        "task_id": {"taskset": "toolathlon_gym:nemo-meal-diagnostic", "task_id": "howtocook-meal-plan-gcal"}, "task_data": {}} for _ in clients]
    checks, active, retained, profiles, counts = [], [], {}, [], []
    marker, marker_raw, before, after = None, None, None, None
    fault_active = False
    def ledger_bytes(name):
        path = VM / name
        return path.read_bytes() if path.exists() else None
    original_before, synthetic_before = ledger_bytes("resource-sessions.json"), ledger_bytes("synthetic-sessions.json")
    def retain(path): retained[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    def check(name, valid):
        checks.append({"name": name, "passed": bool(valid)})
        if not valid: raise AssertionError(name)
    def bound(i): return {k: seeds[i][k] for k in ("resources_session_id", "episode_id")}
    def post(i, route, body, expected=200): return clients[i].post("/isolated" + route, body, expected=expected)
    def measure(i): return post(i, "/verify", {"episode_id": seeds[i]["episode_id"], "task_id": seeds[i]["task_id"],
        "verification_input": {"resources_session_id": seeds[i]["resources_session_id"]}})["result"]
    def tool(i, server, name, arguments, request_id=None):
        result = post(i, "/tool", {**bound(i), "request_id": request_id or str(uuid4()), "server": server, "name": name, "arguments": arguments})
        if result["result"].get("isError"): raise AssertionError("Original MCP tool error")
        return result
    try:
        before = shared_snapshot()
        status_before = bridge_request("/status")
        post(2, "/seed_session", {**seeds[2], "task_id": {**seeds[2]["task_id"], "task_id": "unknown-meal-task"}}, expected=400)
        clients[2].post("/seed_session", seeds[2], expected=400)
        status_after = bridge_request("/status")
        check(CHECKS[0], status_before["isolated"]["current_identities"] == status_after["isolated"]["current_identities"]
            and status_before["sessions"] == status_after["sessions"])
        for i in (0, 1):
            post(i, "/seed_session", seeds[i])
            active.append(i)
            base = ROOT / ".runtime/native-episodes" / seeds[i]["resources_session_id"]
            owner = json.loads((base / "cluster_owner.json").read_text(encoding="utf-8"))
            profile = EpisodeProfile(ROOT, seeds[i]["resources_session_id"], owner["slot"])
            profile.validate_record(owner)
            profiles.append(profile)
        check(CHECKS[1], len({p.host for p in profiles}) == 2 and all(p.resource_id in bridge_request("/status")["isolated"]["active_sessions"] for p in profiles))
        schemas = [post(i, "/tools", bound(i)) for i in (0, 1)]
        expected = {(s, n) for s, names in METHODS.items() for n in names}
        check(CHECKS[2], all({(t["server"], t["name"]) for t in s["tools"]} == expected and s["task_profile"] == TaskRouter(ROOT, "isolated").authorize(seeds[i]) for i, s in enumerate(schemas)))
        post(2, "/tools", bound(0), expected=409)
        post(0, "/tools", {**bound(0), "episode_id": seeds[1]["episode_id"]}, expected=409)
        check(CHECKS[3], True)
        negative = [measure(i) for i in (0, 1)]
        check(CHECKS[4], all(v["reward"] == 0 and v["mask_sample"] is False for v in negative))
        receipts, _, _ = components(ROOT)
        recipes = json.loads((ROOT / receipts["snapshot"]["asset_ref"]).read_text(encoding="utf-8"))
        detail = tool(0, "howtocook", "mcp_howtocook_getRecipeById", {"query": recipes[0]["id"]})
        check(CHECKS[5], json.loads(detail["result"]["content"][0]["text"]) == recipes[0])
        payload, events = public_plan(recipes)
        body = {**bound(0), "request_id": str(uuid4()), "workbook": payload}
        written = post(0, "/write_workbook", body)
        repeated = post(0, "/write_workbook", body)
        downloaded = post(0, "/read_workbook", bound(0))
        check(CHECKS[6], written == repeated and hashlib.sha256(base64.b64decode(downloaded["result"]["base64"], validate=True)).hexdigest() == written["result"]["sha256"])
        request_id = str(uuid4())
        first = tool(0, "google_calendar", "create_event", events[0], request_id)
        repeated = tool(0, "google_calendar", "create_event", events[0], request_id)
        post(0, "/tool", {**bound(0), "request_id": request_id, "server": "google_calendar", "name": "create_event", "arguments": {**events[0], "summary": "rebound"}}, expected=409)
        for event in events[1:]: tool(0, "google_calendar", "create_event", event)
        counts = [require_schema(EpisodeCluster(p)) for p in profiles]
        check(CHECKS[7], counts == [21, 0] and first == repeated)
        positive = measure(0)
        check(CHECKS[8], positive["reward"] == 1 and positive["mask_sample"] is False and all(positive["evaluation"][k] for k in ("upstream_passed", "supplemental_passed")))
        event_id = first["result"]["content"][0]["text"].split("\n", 1)[0].split(": ", 1)[1]
        tool(0, "google_calendar", "update_event", {"eventId": event_id, "summary": "Cook: workagent-diagnostic-wrong-recipe"})
        wrong = measure(0)
        check(CHECKS[9], wrong["reward"] == 0 and wrong["mask_sample"] is False and wrong["evaluation"]["upstream_passed"] is True and wrong["evaluation"]["supplemental_passed"] is False)
        tool(0, "google_calendar", "update_event", {"eventId": event_id, "summary": events[0]["summary"]})
        restored = measure(0)
        check(CHECKS[10], restored["reward"] == 1 and restored["mask_sample"] is False)
        marker = profiles[0].base / "calendar-initialized.json"
        no_links(marker, ROOT)
        marker_raw = marker.read_bytes()
        original = output / "before-infrastructure-fault.json"
        original.write_bytes(marker_raw)
        retain(original)
        save(marker, {"state": "diagnostic-unavailable", "profile": profiles[0].record()})
        fault_active = True
        fault = measure(0)
        check(CHECKS[11], fault["reward"] is None and fault["mask_sample"] is True)
        marker.write_bytes(marker_raw)
        fault_active = False
        save(output / "infrastructure-fault.json", {"kind": "owned_ready_marker_unavailable", "profile": profiles[0].record(),
            "original_ref": original.relative_to(ROOT).as_posix(), "restored_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
            "requests_replayed": 0, "postgres_stopped": False})
        retain(output / "infrastructure-fault.json")
        after_restore = measure(0)
        check(CHECKS[12], after_restore["reward"] == 1 and after_restore["mask_sample"] is False)
        post(0, "/tool", {**bound(0), "request_id": str(uuid4()), "server": "google_calendar", "name": "unexposed_method", "arguments": {}}, expected=400)
        post(0, "/write_report", {**bound(0), "request_id": str(uuid4()), "base64": "Zg=="}, expected=400)
        check(CHECKS[13], True)
        post(0, "/close_session", bound(0))
        active.remove(0)
        other = measure(1)
        check(CHECKS[14], other["reward"] == 0 and other["mask_sample"] is False and require_schema(EpisodeCluster(profiles[1])) == 0)
        post(0, "/seed_session", seeds[0], expected=409)
        closed = measure(0)
        check(CHECKS[15], closed["reward"] is None and closed["mask_sample"] is True)
        post(1, "/close_session", bound(1))
        active.remove(1)
        for profile in profiles: require_dependents_closed(profile)
        check(CHECKS[16], not bridge_request("/status")["isolated"]["active_sessions"] and all(not (p.data / "postmaster.pid").exists() for p in profiles))
        after = shared_snapshot()
        check(CHECKS[17], before == after and original_before == ledger_bytes("resource-sessions.json")
              and synthetic_before == ledger_bytes("synthetic-sessions.json"))
    finally:
        if fault_active: marker.write_bytes(marker_raw)
        for i in reversed(active):
            try: post(i, "/close_session", bound(i))
            except Exception: pass
        for profile in profiles:
            selected = [profile.base / name for name in ("cluster_owner.json", "resources-session.json", "dependent_processes.json")]
            for name in ("meal-task-session", "meal-evaluations"): selected.extend((profile.base / name).rglob("*"))
            for path in selected:
                if path.is_file(): retain(path)
        save(output / "http_journal.json", [event for client in clients for event in client.journal])
        retain(output / "http_journal.json")
        result = {"schema_version": "nemo-original-meal-plan-diagnostic-v1", "at": datetime.now(timezone.utc).isoformat(),
            "framework": "nemo_gym", "execution_kind": "scripted_original_task_diagnostic", "nemo_second_task_enabled": True,
            "passed": [c["name"] for c in checks] == list(CHECKS) and all(c["passed"] for c in checks), "checks": checks,
            "retained_sha256": retained, "journal_ref": (output / "http_journal.json").relative_to(ROOT).as_posix(),
            "fault_ref": (output / "infrastructure-fault.json").relative_to(ROOT).as_posix(), "calendar_counts": counts,
            "shared_before": before, "shared_after": after, "source_sha256": {**expected_identity(ROOT),
                **{name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in EXTRA_SOURCES}},
            "training_eligible": False, "training_admissions": 0, "real_model_calls": 0, "training_updates": 0,
            "reference_solutions_read": False, "verified_task_bodies_read": False}
        save(output / "result.json", result)
        print(json.dumps({"passed": result["passed"], "checks": len(checks), "receipt": (output / "result.json").relative_to(ROOT).as_posix()}))
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
