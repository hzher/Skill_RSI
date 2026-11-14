"""Run both original task types in the same NeMo isolated resource pool."""
from datetime import datetime, timezone
import hashlib
import json
import sys
from uuid import uuid4

from mainline_runtime import ROOT, VM, bridge_request
sys.path.insert(0, str(ROOT))
from diagnostic_tax_solution import solve
from diagnostic_meal_plan import public_plan
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile, require_dependents_closed
from integrations.meal_plan_services import components
from integrations.nemo_http import NeMoHTTP
from integrations.nemo_mixed_audit import CHECKS, EXTRA_SOURCES
from integrations.runtime_identity import expected_identity
from integrations.task_routing import TaskRouter
from scripts.verify_calendar_isolation import shared_snapshot
from workenv.core import save


def schemas(profile):
    conn = EpisodeCluster(profile).connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT nspname FROM pg_namespace WHERE nspname IN ('wc','gsheet','gcal') ORDER BY nspname")
            return [row[0] for row in cur.fetchall()]
    finally: conn.close()


def main():
    output = ROOT / "evidence/mainline" / ("nemo-mixed-tasks-" + uuid4().hex)
    output.mkdir()
    clients = [NeMoHTTP() for _ in range(3)]
    tasks = ["wc-sales-tax-summary", "howtocook-meal-plan-gcal", "howtocook-meal-plan-gcal"]
    seeds = [{"resources_session_id": str(uuid4()), "episode_id": {"rollout_id": "mixed-" + uuid4().hex, "attempt": 0},
        "task_id": {"taskset": "toolathlon_gym:nemo-mixed-diagnostic", "task_id": task}, "task_data": {}} for task in tasks]
    checks, active, profiles, retained, measurements = [], [], {}, {}, []
    before, after, observed_schemas, reused_schemas = None, None, [], None
    def optional_ledger_bytes(name):
        path = VM / name
        return path.read_bytes() if path.exists() else None
    original_before, synthetic_before = (VM / "resource-sessions.json").read_bytes(), optional_ledger_bytes("synthetic-sessions.json")
    def check(name, valid):
        checks.append({"name": name, "passed": bool(valid)})
        if not valid: raise AssertionError(name)
    def bound(i): return {k: seeds[i][k] for k in ("resources_session_id", "episode_id")}
    def post(i, route, body, expected=200): return clients[i].post("/isolated" + route, body, expected=expected)
    def measure(i):
        result = post(i, "/verify", {"episode_id": seeds[i]["episode_id"], "task_id": seeds[i]["task_id"],
            "verification_input": {"resources_session_id": seeds[i]["resources_session_id"]}})["result"]
        measurements.append({"task_index": i, "result": result})
        return result
    def seed(i):
        post(i, "/seed_session", seeds[i])
        active.append(i)
        base = ROOT / ".runtime/native-episodes" / seeds[i]["resources_session_id"]
        owner = json.loads((base / "cluster_owner.json").read_text(encoding="utf-8"))
        profile = EpisodeProfile(ROOT, seeds[i]["resources_session_id"], owner["slot"])
        profile.validate_record(owner)
        profiles[i] = profile
    def tool(i, server, name, arguments):
        result = post(i, "/tool", {**bound(i), "request_id": str(uuid4()), "server": server, "name": name, "arguments": arguments})
        if server == "google_calendar" and result["result"].get("isError"): raise AssertionError("Original calendar fixture write failed")
        return result
    def retain(path): retained[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        before = shared_snapshot()
        seed(0)
        seed(1)
        check(CHECKS[0], set(bridge_request("/status")["isolated"]["active_sessions"]) == {s["resources_session_id"] for s in seeds[:2]})
        discovered = [post(i, "/tools", bound(i)) for i in (0, 1)]
        routes = [TaskRouter(ROOT, "isolated").authorize(s) for s in seeds[:2]]
        check(CHECKS[1], all(d["task_profile"] == route and {(t["server"], t["name"]) for t in d["tools"]}
            == {(s, n) for s, names in route["mcp_methods"].items() for n in names} for d, route in zip(discovered, routes)))
        observed_schemas = [schemas(profiles[i]) for i in (0, 1)]
        check(CHECKS[2], observed_schemas == [["gsheet", "wc"], ["gcal"]])
        identities = bridge_request("/status")["isolated"]["current_identities"]
        post(2, "/seed_session", seeds[2], expected=409)
        check(CHECKS[3], bridge_request("/status")["isolated"]["current_identities"] == identities)
        for i, server, name in ((0, "google_calendar", "list_events"), (1, "woocommerce", "woo_orders_list")):
            post(i, "/tool", {**bound(i), "request_id": str(uuid4()), "server": server, "name": name, "arguments": {}}, expected=400)
        check(CHECKS[4], True)
        empty = [measure(i) for i in (0, 1)]
        check(CHECKS[5], all(m["reward"] == 0 and m["mask_sample"] is False for m in empty))
        solve(lambda route, body: post(0, route, body), bound(0))
        receipts, _, _ = components(ROOT)
        recipes = json.loads((ROOT / receipts["snapshot"]["asset_ref"]).read_text(encoding="utf-8"))
        payload, events = public_plan(recipes)
        post(1, "/write_workbook", {**bound(1), "request_id": str(uuid4()), "workbook": payload})
        for event in events: tool(1, "google_calendar", "create_event", event)
        positive = [measure(i) for i in (0, 1)]
        check(CHECKS[6], all(m["reward"] == 1 and m["mask_sample"] is False and all(m["evaluation"][key] is True for key in ("upstream_passed", "supplemental_passed")) for m in positive))
        post(0, "/close_session", bound(0))
        active.remove(0)
        check(CHECKS[7], measure(1)["reward"] == 1)
        seed(2)
        reused_schemas = schemas(profiles[2])
        check(CHECKS[8], profiles[2].slot == profiles[0].slot and profiles[2].data != profiles[0].data and reused_schemas == ["gcal"])
        check(CHECKS[9], measure(2)["reward"] == 0 and measure(1)["reward"] == 1)
        for i in (2, 1):
            post(i, "/close_session", bound(i))
            active.remove(i)
        for i in (0, 1, 2):
            post(i, "/seed_session", seeds[i], expected=409)
            require_dependents_closed(profiles[i])
        check(CHECKS[10], all(not (p.data / "postmaster.pid").exists() for p in profiles.values()) and not bridge_request("/status")["isolated"]["active_sessions"])
        after = shared_snapshot()
        check(CHECKS[11], before == after and original_before == (VM / "resource-sessions.json").read_bytes() and synthetic_before == optional_ledger_bytes("synthetic-sessions.json"))
    finally:
        for i in reversed(active):
            try: post(i, "/close_session", bound(i))
            except Exception: pass
        for i, profile in profiles.items():
            selected = [profile.base / name for name in ("cluster_owner.json", "resources-session.json", "dependent_processes.json")]
            if tasks[i] == "howtocook-meal-plan-gcal":
                for name in ("meal-task-session", "meal-evaluations"): selected.extend((profile.base / name).rglob("*"))
            else:
                runs = {m["result"].get("run_ref") for m in measurements if m["task_index"] == i}
                for ref in runs:
                    if not ref: continue
                    run = ROOT / ref
                    selected.extend(run / name for name in ("run.json", "initial_state.json", "nemo_binding.json", "task_profile.json", "nemo_closed.json"))
                    for directory in run.glob("verify_*"): selected.extend(directory.iterdir())
            for path in selected:
                if path.is_file(): retain(path)
        save(output / "http_journal.json", [event for client in clients for event in client.journal])
        retain(output / "http_journal.json")
        result = {"schema_version": "nemo-mixed-original-tasks-diagnostic-v1", "at": datetime.now(timezone.utc).isoformat(),
            "passed": [c["name"] for c in checks] == list(CHECKS) and all(c["passed"] for c in checks), "checks": checks,
            "framework": "nemo_gym", "execution_kind": "scripted_heterogeneous_resource_diagnostic",
            "seeds": seeds, "profiles": {str(i): p.record() for i, p in profiles.items()}, "sql_schemas": observed_schemas,
            "reused_slot_schemas": reused_schemas, "measurements": measurements,
            "journal_ref": (output / "http_journal.json").relative_to(ROOT).as_posix(), "retained_sha256": retained,
            "shared_before": before, "shared_after": after, "real_model_calls": 0, "training_updates": 0,
            "training_admissions": 0, "training_eligible": False, "reference_solutions_read": False, "verified_task_bodies_read": False,
            "source_sha256": {**expected_identity(ROOT), **{name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in EXTRA_SOURCES}}}
        save(output / "result.json", result)
        print(json.dumps({"passed": result["passed"], "checks": len(checks), "receipt": (output / "result.json").relative_to(ROOT).as_posix()}))
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
