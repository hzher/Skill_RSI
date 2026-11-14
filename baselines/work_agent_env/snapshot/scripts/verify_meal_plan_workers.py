"""Exercise two original meal-plan tasks through the existing private worker IPC."""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adapters import toolathlon_native as native
from integrations.calendar_component import require_schema
from integrations.episode_cluster import EpisodeCluster, require_dependents_closed
from integrations.episode_worker_client import EpisodeWorkerClient
from integrations.meal_plan_services import METHODS, components
from integrations.meal_worker_audit import CHECKS, SOURCES
from integrations.task_routing import TaskRouter
from scripts.verify_calendar_isolation import shared_snapshot
from workenv.core import save
from workenv.postgres_binary import postgres_binary
from diagnostic_meal_plan import public_plan


def main():
    output = ROOT / "evidence/mainline" / ("meal-workers-probe-" + uuid4().hex)
    output.mkdir()
    private = ROOT / ".runtime/native-episodes" / output.name
    private.mkdir()
    workers, checks, journal, retained, counts, after_close = [], [], [], {}, [], []
    before, after = None, None
    def check(name, valid):
        checks.append({"name": name, "passed": bool(valid)})
        if not valid: raise AssertionError(name)
    def retain(path): retained[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    def bound(worker): return {"resources_session_id": worker.profile.resource_id,
        "episode_id": {"rollout_id": "private-meal-" + worker.profile.resource_id, "attempt": 0}}
    def call(worker, route, body, status=200):
        reply = worker.call(route, body)
        journal.append({"slot": worker.profile.slot, "route": route, "request": body, "response": reply})
        if reply["status"] != status: raise AssertionError("Unexpected private meal worker reply " + str(reply["status"]))
        return reply.get("result")
    def tool(worker, server, name, arguments, request_id=None):
        result = call(worker, "/tool", {**bound(worker), "request_id": request_id or str(uuid4()), "server": server, "name": name, "arguments": arguments})
        if result["result"].get("isError"): raise AssertionError("Original business MCP returned a tool error")
        return result
    try:
        before = shared_snapshot()
        receipts, _, _ = components(ROOT)
        recipes = json.loads((ROOT / receipts["snapshot"]["asset_ref"]).read_text(encoding="utf-8"))
        seed = private / "calendar-schema.sql"
        dump = subprocess.run([str(postgres_binary(ROOT, "pg_dump")), "--host=127.0.0.1", "--port=5432", "--username=eigent",
            "--dbname=toolathlon_gym", "--schema=gcal", "--schema-only", "--no-owner", "--no-privileges", "--file=" + str(seed)],
            env=native.env(), capture_output=True, timeout=30)
        if dump.returncode or not 0 < seed.stat().st_size < 256 * 1024 or b"COPY gcal.events" in seed.read_bytes(): raise RuntimeError("Calendar schema-only seed failed")
        retain(seed)
        for slot in (0, 1): workers.append(EpisodeWorkerClient(ROOT, slot=slot, seed_sql=seed, task_id="howtocook-meal-plan-gcal"))
        check(CHECKS[0], workers[0].ready["pid"] != workers[1].ready["pid"] and workers[0].profile.host != workers[1].profile.host
            and all(w.ready["task_id"] == "howtocook-meal-plan-gcal" for w in workers))
        seeds, schemas, empty = [], [], []
        for worker in workers:
            seed_body = {**bound(worker), "task_id": {"taskset": "toolathlon_gym:private-meal-worker-diagnostic", "task_id": "howtocook-meal-plan-gcal"}, "task_data": {}}
            seeds.append(seed_body)
            call(worker, "/seed_session", seed_body)
            schemas.append(call(worker, "/tools", bound(worker)))
            empty.append(call(worker, "/verify", bound(worker)))
        expected = {(s, n) for s, names in METHODS.items() for n in names}
        check(CHECKS[1], all({(t["server"], t["name"]) for t in schema["tools"]} == expected
            and schema["task_profile"] == TaskRouter(ROOT, "isolated").authorize(seed_body)
            and {a["name"] for a in schema["artifact_tools"]} == {"write_workbook", "read_workbook"} for schema, seed_body in zip(schemas, seeds)))
        check(CHECKS[2], all(e["reward"] == 0 and e["mask_sample"] is False for e in empty))
        one, two = workers
        detail = tool(one, "howtocook", "mcp_howtocook_getRecipeById", {"query": recipes[0]["id"]})
        check(CHECKS[3], json.loads(detail["result"]["content"][0]["text"]) == recipes[0])
        payload, events = public_plan(recipes)
        workbook_body = {**bound(one), "request_id": str(uuid4()), "workbook": payload}
        written = call(one, "/write_workbook", workbook_body)
        repeated = call(one, "/write_workbook", workbook_body)
        downloaded = call(one, "/read_workbook", bound(one))
        check(CHECKS[4], written == repeated and hashlib.sha256(base64.b64decode(downloaded["result"]["base64"], validate=True)).hexdigest() == written["result"]["sha256"])
        request_id = str(uuid4())
        first = tool(one, "google_calendar", "create_event", events[0], request_id)
        replay = tool(one, "google_calendar", "create_event", events[0], request_id)
        call(one, "/tool", {**bound(one), "request_id": request_id, "server": "google_calendar", "name": "create_event", "arguments": {**events[0], "summary": "rebound must fail"}}, 409)
        for event in events[1:]: tool(one, "google_calendar", "create_event", event)
        check(CHECKS[5], first == replay)
        counts = [require_schema(EpisodeCluster(w.profile)) for w in workers]
        check(CHECKS[6], counts == [21, 0])
        positive = call(one, "/verify", bound(one))
        check(CHECKS[7], positive["reward"] == 1 and positive["mask_sample"] is False
            and all(positive["evaluation"][k] is True for k in ("upstream_passed", "supplemental_passed")))
        call(one, "/tool", {**bound(one), "request_id": str(uuid4()), "server": "filesystem", "name": "read_file", "arguments": {}}, 400)
        call(one, "/write_report", {**bound(one), "request_id": str(uuid4()), "base64": "Zg=="}, 400)
        check(CHECKS[8], True)
        call(one, "/verify", bound(two), 400)
        call(one, "/read_workbook", {**bound(one), "episode_id": {"rollout_id": "different", "attempt": 0}}, 409)
        check(CHECKS[9], True)
        call(one, "/close_session", bound(one))
        after_close = [require_schema(EpisodeCluster(w.profile)) for w in workers]
        archive = json.loads((one.profile.base / "meal-task-session/closed-artifact.json").read_text(encoding="utf-8"))
        check(CHECKS[10], after_close == [0, 0] and hashlib.sha256((ROOT / archive["ref"]).read_bytes()).hexdigest() == written["result"]["sha256"])
        other = call(two, "/verify", bound(two))
        check(CHECKS[11], other["reward"] == 0 and other["mask_sample"] is False)
        call(two, "/close_session", bound(two))
        for worker, seed_body in zip(workers, seeds):
            call(worker, "/seed_session", seed_body, 409)
            call(worker, "/close_session", bound(worker))
        check(CHECKS[12], True)
        for worker in reversed(workers): worker.stop()
        for worker in workers: require_dependents_closed(worker.profile)
        check(CHECKS[13], all(w.process.poll() is not None and not (w.profile.data / "postmaster.pid").exists() for w in workers))
        after = shared_snapshot()
        check(CHECKS[14], before == after)
    finally:
        for worker in reversed(workers):
            if worker.process.poll() is None: worker.stop()
        for worker in workers:
            base = worker.profile.base
            selected_paths = [base / name for name in ("cluster_owner.json", "postmaster_identity.json", "resources-session.json",
                "calendar-initialized.json", "dependent_processes.json")]
            selected_paths.extend((base / "meal-task-session").rglob("*"))
            selected_paths.extend((base / "meal-evaluations").rglob("*"))
            for directory in base.glob("meal-services-*"): selected_paths.extend(directory.iterdir())
            selected_paths.extend(base.glob("meal-artifact-write-*.json"))
            for path in selected_paths:
                if path.is_file(): retain(path)
        save(output / "resource_journal.json", journal)
        retain(output / "resource_journal.json")
        result = {"schema_version": "meal-plan-private-workers-diagnostic-v1", "at": datetime.now(timezone.utc).isoformat(),
            "passed": [c["name"] for c in checks] == list(CHECKS) and all(c["passed"] for c in checks), "checks": checks,
            "execution_kind": "private_original_meal_workers_diagnostic", "profiles": [w.profile.record() for w in workers],
            "journal_ref": (output / "resource_journal.json").relative_to(ROOT).as_posix(), "retained_sha256": retained,
            "calendar_counts": counts, "after_first_close_counts": after_close, "shared_before": before, "shared_after": after,
            "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES},
            "nemo_second_task_enabled": False, "training_eligible": False, "real_model_calls": 0, "training_updates": 0,
            "reference_solutions_read": False, "verified_task_bodies_read": False}
        save(output / "result.json", result)
        print(json.dumps({"passed": result["passed"], "checks": len(checks), "receipt": (output / "result.json").relative_to(ROOT).as_posix()}))
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
