"""Hard-fault one attested meal worker; explicitly reconcile before PG recovery."""
from datetime import datetime, timezone
import hashlib
import json
import sys
import time
from uuid import uuid4

from mainline_runtime import ROOT, VM, bridge_request
sys.path.insert(0, str(ROOT))
from verify_meal_plan_recovery import AttestedChild, event_rows
if sys.platform == "linux":
    from workenv.attested_process import LinuxAttestedChild as AttestedChild
from integrations.dependent_reconciliation import reconcile_dependents
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile, recover_orphan_cluster, require_dependents_closed
from integrations.meal_worker_recovery_audit import CHECKS, EXTRA_SOURCES
from integrations.nemo_http import NeMoHTTP
from integrations.runtime_identity import expected_identity
from integrations.task_routing import TaskRouter
from scripts.verify_calendar_isolation import shared_snapshot
from workenv.core import ResourceError, no_links, save
from workenv.process_identity import process_identity


def main():
    output = ROOT / "evidence/mainline" / ("nemo-meal-worker-fault-" + uuid4().hex)
    output.mkdir()
    client, fresh_client = NeMoHTTP(), NeMoHTTP()
    sid, fresh = str(uuid4()), str(uuid4())
    episode = {"rollout_id": "meal-worker-fault-" + uuid4().hex, "attempt": 0}
    task = {"taskset": "toolathlon_gym:meal-worker-fault", "task_id": "howtocook-meal-plan-gcal"}
    bound = {"resources_session_id": sid, "episode_id": episode}
    seed = {**bound, "task_id": task, "task_data": {}}
    checks, retained, proof, child_handles = [], {}, {}, {}
    worker_handle, profile, cluster, ready, before, after = None, None, None, False, None, None
    cleanup_errors = []
    def check(name, value):
        checks.append({"name": name, "passed": bool(value)})
        if not value: raise AssertionError(name)
    def retain(path): retained[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    def snapshot(name, path):
        target = output / name
        target.write_bytes(path.read_bytes())
        retain(target)
        return target.relative_to(ROOT).as_posix()
    def measure(target, identifier):
        return target.post("/isolated/verify", {"episode_id": episode, "task_id": task,
            "verification_input": {"resources_session_id": identifier}})["result"]
    try:
        initial = bridge_request("/status")
        if initial["active_session"] or initial["isolated"]["active_sessions"] or initial["synthetic"]["active_sessions"]:
            raise ResourceError("Close resources before the owned worker fault diagnostic")
        before = shared_snapshot()
        client.post("/isolated/seed_session", seed)
        ready = True
        base = ROOT / ".runtime/native-episodes" / sid
        owner = json.loads((base / "cluster_owner.json").read_text(encoding="utf-8"))
        profile = EpisodeProfile(ROOT, sid, owner["slot"])
        profile.validate_record(owner)
        cluster = EpisodeCluster(profile)
        cluster.require_owner()
        route = json.loads((base / "meal-task-session/task_profile.json").read_text(encoding="utf-8"))
        if route != TaskRouter(ROOT, "isolated").authorize(seed): raise ResourceError("Fault target has another source profile")
        workbook = client.post("/isolated/write_workbook", {**bound, "request_id": str(uuid4()), "workbook": {"meal_plan": [], "shopping_list": []}})
        mutation = {**bound, "request_id": str(uuid4()), "server": "google_calendar", "name": "create_event", "arguments": {
            "summary": "Owned worker fault " + sid, "start": {"dateTime": "2026-03-09T08:00:00+08:00", "timeZone": "Asia/Shanghai"},
            "end": {"dateTime": "2026-03-09T09:00:00+08:00", "timeZone": "Asia/Shanghai"}}}
        result = client.post("/isolated/tool", mutation)
        proof["events_before"] = event_rows(cluster)
        check(CHECKS[0], not result["result"].get("isError") and len(proof["events_before"]) == 1)
        slotfile, leasefile = base.parent / ("slot-" + str(profile.slot) + ".json"), base.parent / ("slot-" + str(profile.slot) + ".lock")
        dependent = json.loads((base / "dependent_processes.json").read_text(encoding="utf-8"))
        lease = json.loads(leasefile.read_text(encoding="utf-8"))
        proof["worker_before"] = lease["process"]
        if (lease.get("workspace_owner") != str(ROOT) or lease.get("kind") != "native_episode_slot"
            or lease.get("resource") != leasefile.relative_to(ROOT).as_posix()
            or process_identity(lease["process"]["pid"]) != lease["process"]
            or dependent["profile"] != profile.record() or dependent["state"] != "open"
            or set(dependent["children"]) != {"howtocook", "google_calendar"}):
            raise ResourceError("Worker or MCP process ownership differs")
        worker_handle = AttestedChild(lease["process"])
        for name, child in dependent["children"].items():
            if child["profile"] != profile.record() or process_identity(child["before"]["pid"]) != child["before"]:
                raise ResourceError("Original MCP child identity changed")
            child_handles[name] = AttestedChild(child["before"])
        check(CHECKS[1], len(child_handles) == 2)
        proof["originals"] = {"dependents": snapshot("before-dependents.json", base / "dependent_processes.json"),
            "lease": snapshot("before-slot-lease.json", leasefile), "slot": snapshot("before-slot.json", slotfile),
            "worker_ledger": snapshot("before-worker-ledger.json", base / "resources-session.json")}
        proof["worker_fault_injected"] = worker_handle.terminate_if_alive()
        proof["worker_after"] = process_identity(lease["process"]["pid"])
        check(CHECKS[2], proof["worker_fault_injected"] is True and proof["worker_after"]["status"] in ("not_found", "exited"))
        unknown = measure(client, sid)
        check(CHECKS[3], unknown["reward"] is None and unknown["mask_sample"] is True)
        client.post("/isolated/tool", mutation, expected=503)
        check(CHECKS[4], event_rows(cluster) == proof["events_before"])
        try: recover_orphan_cluster(ROOT, resource_id=sid, slot=profile.slot)
        except ResourceError: blocked = True
        else: blocked = False
        check(CHECKS[5], blocked and slotfile.read_bytes() == (ROOT / proof["originals"]["slot"]).read_bytes()
            and leasefile.read_bytes() == (ROOT / proof["originals"]["lease"]).read_bytes() and (profile.data / "postmaster.pid").exists())
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if all(process_identity(child["before"]["pid"])["status"] in ("not_found", "exited") for child in dependent["children"].values()): break
            time.sleep(0.1)
        proof["diagnostic_child_terminations"] = [name for name, handle in child_handles.items() if handle.terminate_if_alive()]
        proof["children_after"] = {name: process_identity(child["before"]["pid"]) for name, child in dependent["children"].items()}
        check(CHECKS[6], all(identity["status"] in ("not_found", "exited") for identity in proof["children_after"].values()))
        proof["reconciliation"] = reconcile_dependents(ROOT, resource_id=sid, slot=profile.slot, recover_stale_locks=True)
        check(CHECKS[7], proof["reconciliation"]["slot_released"] is False and proof["reconciliation"]["processes_terminated"] == 0
            and proof["reconciliation"]["requests_replayed"] == 0 and (profile.data / "postmaster.pid").exists()
            and slotfile.read_bytes() == (ROOT / proof["originals"]["slot"]).read_bytes()
            and leasefile.read_bytes() == (ROOT / proof["originals"]["lease"]).read_bytes())
        proof["events_after_reconciliation"] = event_rows(cluster)
        artifact = base / "calendar-workspace/Weekly_Meal_Plan.xlsx"
        no_links(artifact, ROOT)
        proof["workbook_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        proof["workbook_ref"] = artifact.relative_to(ROOT).as_posix()
        check(CHECKS[8], proof["events_after_reconciliation"] == proof["events_before"] and proof["workbook_sha256"] == workbook["result"]["sha256"]
            and (base / "resources-session.json").read_bytes() == (ROOT / proof["originals"]["worker_ledger"]).read_bytes())
        client.post("/isolated/close_session", bound)
        client.post("/isolated/seed_session", seed, expected=409)
        closed = measure(client, sid)
        row = json.loads((VM / "isolated-sessions.json").read_text(encoding="utf-8"))["sessions"][sid]
        proof["gateway_closed_row"] = row
        check(CHECKS[9], row["state"] == "closed" and closed["reward"] is None and closed["mask_sample"] is True
            and not (profile.data / "postmaster.pid").exists() and not leasefile.exists())
        fresh_client.post("/isolated/seed_session", {**seed, "resources_session_id": fresh})
        fresh_result = measure(fresh_client, fresh)
        fresh_client.post("/isolated/close_session", {**bound, "resources_session_id": fresh})
        check(CHECKS[10], fresh_result["reward"] == 0 and fresh_result["mask_sample"] is False)
        after = shared_snapshot()
        check(CHECKS[11], before == after and not bridge_request("/status")["isolated"]["active_sessions"])
    finally:
        try:
            if worker_handle is not None: worker_handle.terminate_if_alive()
            for handle in child_handles.values(): handle.terminate_if_alive()
            if profile is not None and set(child_handles) == {"howtocook", "google_calendar"}:
                try: require_dependents_closed(profile)
                except ResourceError: reconcile_dependents(ROOT, resource_id=sid, slot=profile.slot, recover_stale_locks=True)
            if ready:
                client.post("/isolated/close_session", bound)
                fresh_client.post("/isolated/close_session", {**bound, "resources_session_id": fresh})
        except Exception as exc: cleanup_errors.append(type(exc).__name__ + ": " + str(exc))
        finally:
            if worker_handle is not None: worker_handle.close()
            for handle in child_handles.values(): handle.close()
        if profile is not None:
            for name in ("cluster_owner.json", "dependent_processes.json", "resources-session.json", "calendar-workspace/Weekly_Meal_Plan.xlsx",
                "meal-task-session/task_profile.json", "meal-task-session/nemo_binding.json"):
                path = profile.base / name
                if path.is_file(): retain(path)
            for path in (profile.base / "dependent-record-history").glob("*"):
                if path.is_file(): retain(path)
            for path in profile.base.glob("recovered-slot-lease-*.json"): retain(path)
        save(output / "fault_observations.json", proof)
        retain(output / "fault_observations.json")
        save(output / "http_journal.json", client.journal + fresh_client.journal)
        retain(output / "http_journal.json")
        result = {"schema_version": "nemo-meal-hard-worker-fault-diagnostic-v1", "at": datetime.now(timezone.utc).isoformat(),
            "passed": [c["name"] for c in checks] == list(CHECKS) and all(c["passed"] for c in checks) and not cleanup_errors, "checks": checks,
            "profile": profile.record() if profile else None, "seed": seed, "fresh_resource_id": fresh,
            "cleanup_errors": cleanup_errors, "execution_kind": "owned_private_worker_fault_and_explicit_reconciliation",
            "observations_ref": (output / "fault_observations.json").relative_to(ROOT).as_posix(),
            "journal_ref": (output / "http_journal.json").relative_to(ROOT).as_posix(), "retained_sha256": retained,
            "shared_before": before, "shared_after": after, "real_model_calls": 0, "training_updates": 0, "training_admissions": 0,
            "training_eligible": False, "reference_solutions_read": False, "verified_task_bodies_read": False,
            "source_sha256": {**expected_identity(ROOT), **{name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in EXTRA_SOURCES}}}
        save(output / "result.json", result)
        print(json.dumps({"passed": result["passed"], "checks": len(checks), "receipt": (output / "result.json").relative_to(ROOT).as_posix()}))
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
