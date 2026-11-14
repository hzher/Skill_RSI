"""Fault a project-owned original MCP controller and reconcile its dependents.

This is a local resource diagnostic, not a NeMo session recovery acceptance.
Only handles attested before the fault may be terminated by the diagnostic.
The production coordinator never terminates processes or replays requests.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adapters import toolathlon_native as native
from adapters.meal_plan_artifacts import write_report
from integrations.calendar_component import initialize
from integrations.dependent_reconciliation import reconcile_dependents
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile, recover_orphan_cluster, reserve_cluster
from integrations.meal_plan_services import MealPlanServices
from integrations.meal_recovery_audit import CHECKS, SOURCES
from scripts.verify_calendar_isolation import shared_snapshot
from workenv.core import ResourceError, contained, save
from workenv.process_identity import process_identity
from workenv.postgres_binary import postgres_binary
if sys.platform == "linux":
    from workenv.attested_process import LinuxAttestedChild


class AttestedChild:
    """Keep a Windows handle to an already identified diagnostic child."""
    def __init__(self, identity):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.identity = ctypes, identity
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x1000 | 0x00100000 | 1, False, identity["pid"])
        if not self.handle: raise ResourceError("Cannot open the identified diagnostic child")
        try:
            values = [wintypes.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(self.handle, *[ctypes.byref(v) for v in values]):
                raise ResourceError("Cannot attest the diagnostic child handle")
            created = (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
            if identity["status"] != "alive" or identity["creation_id"] != "win32-filetime:" + str(created):
                raise ResourceError("Diagnostic child handle belongs to another creation identity")
        except BaseException:
            self.close()
            raise

    def terminate_if_alive(self):
        status = self.kernel.WaitForSingleObject(self.handle, 0)
        if status == 0: return False
        if status != 0x102: raise ResourceError("Diagnostic child handle has unknown state")
        if not self.kernel.TerminateProcess(self.handle, 91): raise ResourceError("Owned diagnostic child termination failed")
        if self.kernel.WaitForSingleObject(self.handle, 10000) != 0: raise ResourceError("Owned diagnostic child did not exit")
        return True

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


if sys.platform == "linux":
    AttestedChild = LinuxAttestedChild


def event_rows(cluster):
    conn = cluster.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT row_to_json(e)::text FROM gcal.events e ORDER BY id")
            return [json.loads(row[0]) for row in cur.fetchall()]
    finally: conn.close()


def controller(resource_id, seed_ref, ready_ref):
    seed, ready = contained(ROOT, seed_ref), contained(ROOT, ready_ref)
    with reserve_cluster(ROOT, slot=0, resource_id=resource_id) as cluster:
        cluster.start(seed)
        build = json.loads((ROOT / "evidence/mainline/calendar_build.json").read_text(encoding="utf-8"))
        workspace = initialize(ROOT, build, cluster)
        artifact = write_report(ROOT, cluster, {"meal_plan": [], "shopping_list": []})
        async def run():
            services = MealPlanServices(ROOT, cluster)
            async with services.open():
                journal = []
                recipes = json.loads(contained(ROOT, services.receipts["snapshot"]["asset_ref"]).read_text(encoding="utf-8"))
                for server, name, arguments in (
                    ("howtocook", "mcp_howtocook_getRecipeById", {"query": recipes[0]["id"]}),
                    ("google_calendar", "create_event", {"summary": "Owned recovery diagnostic " + resource_id,
                        "start": {"dateTime": "2026-03-09T08:00:00+08:00", "timeZone": "Asia/Shanghai"},
                        "end": {"dateTime": "2026-03-09T09:00:00+08:00", "timeZone": "Asia/Shanghai"}})):
                    response = await services.call(server, name, arguments)
                    if response.get("isError"): raise ResourceError("Original recovery diagnostic tool failed")
                    journal.append({"server": server, "name": name, "arguments": arguments, "response": response})
                if journal[-1]["response"].get("isError"): raise ResourceError("Original diagnostic event creation failed")
                save(ready, {"profile": cluster.profile.record(), "controller": process_identity(os.getpid()), "launcher_parent_pid": os.getppid(),
                    "services": services.record(), "directory_ref": services.directory.relative_to(ROOT).as_posix(),
                    "artifact": artifact, "journal": journal, "events": event_rows(cluster),
                    "workbook_ref": (workspace / "Weekly_Meal_Plan.xlsx").relative_to(ROOT).as_posix()})
                # A surviving controller cleans up normally if the parent disappears.
                await asyncio.wait_for(asyncio.Event().wait(), timeout=180)
        asyncio.run(run())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--resource-id", help=argparse.SUPPRESS)
    parser.add_argument("--seed-ref", help=argparse.SUPPRESS)
    parser.add_argument("--ready-ref", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.controller:
        controller(args.resource_id, args.seed_ref, args.ready_ref)
        return 0
    if sys.platform not in ("win32", "linux"):
        raise ResourceError("This process fault diagnostic requires a supported resource host")
    output = ROOT / "evidence/mainline" / ("meal-recovery-probe-" + uuid4().hex)
    output.mkdir()
    private = ROOT / ".runtime/native-episodes" / output.name
    private.mkdir()
    resource_id = str(uuid4())
    profile = EpisodeProfile(ROOT, resource_id, 0)
    cluster = EpisodeCluster(profile)
    checks, proof, retained, handles = [], {}, {}, {}
    child, ready, original_slot, original_dependents, controller_handle = None, None, None, None, None
    cleanup_errors = []
    def check(name, valid):
        checks.append({"name": name, "passed": bool(valid)})
        if not valid: raise AssertionError(name)
    def retain(path): retained[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    def snapshot(name, source):
        target = private / name
        target.write_bytes(source.read_bytes())
        retain(target)
        return target.relative_to(ROOT).as_posix()
    def refused(action):
        before = {p: p.read_bytes() for p in (profile.base / "dependent_processes.json", slotfile, slot_lock)}
        try: action()
        except (ResourceError, RuntimeError): pass
        else: return False
        return all(p.read_bytes() == raw for p, raw in before.items())
    slotfile = profile.base.parent / "slot-0.json"
    slot_lock = profile.base.parent / "slot-0.lock"
    try:
        proof["shared_before"] = shared_snapshot()
        seed = private / "calendar-schema.sql"
        dump = subprocess.run([str(postgres_binary(ROOT, "pg_dump")), "--host=127.0.0.1", "--port=5432", "--username=eigent",
            "--dbname=toolathlon_gym", "--schema=gcal", "--schema-only", "--no-owner", "--no-privileges", "--file=" + str(seed)],
            env=native.env(), capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        (private / "dump.stderr.txt").write_bytes(dump.stderr)
        if dump.returncode or not 0 < seed.stat().st_size < 256 * 1024 or b"COPY gcal.events" in seed.read_bytes():
            raise ResourceError("Owned calendar schema-only dump failed")
        retain(seed)
        ready_path = private / "controller-ready.json"
        with (private / "controller.stdout.txt").open("xb") as out, (private / "controller.stderr.txt").open("xb") as err:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--controller", "--resource-id", resource_id,
                "--seed-ref", seed.relative_to(ROOT).as_posix(), "--ready-ref", ready_path.relative_to(ROOT).as_posix()],
                cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"}, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deadline = time.monotonic() + 50
            while not ready_path.is_file() and child.poll() is None and time.monotonic() < deadline: time.sleep(0.1)
            if not ready_path.is_file(): raise ResourceError("Original MCP controller did not become ready; inspect retained stderr")
            ready = json.loads(ready_path.read_text(encoding="utf-8"))
            retain(ready_path)
            proof["ready_ref"] = ready_path.relative_to(ROOT).as_posix()
            profile.validate_record(ready["profile"])
            # Windows venv python.exe may be a redirector retaining a different
            # PID. Attest the real controller against its lease and parent first.
            proof["launcher_identity"] = process_identity(child.pid)
            if (ready["controller"] != process_identity(ready["controller"]["pid"])
                or (ready["controller"]["pid"] != child.pid and ready["launcher_parent_pid"] != child.pid)
                or json.loads(slot_lock.read_text(encoding="utf-8"))["process"] != ready["controller"]):
                raise ResourceError("Controller identity or owned launcher/lease binding changed")
            controller_handle = AttestedChild(ready["controller"])
            sessions = ready["services"]["sessions"]
            for name, session in sessions.items(): handles[name] = AttestedChild(session["before"])
            check(CHECKS[0], set(sessions) == {"howtocook", "google_calendar"} and len({s["before"]["pid"] for s in sessions.values()}) == 2
                and ready["services"]["state"] == "open" and ready["services"]["calls"] == 2 and len(ready["events"]) == 1)
            original_slot = slotfile.read_bytes()
            original_dependents = (profile.base / "dependent_processes.json").read_bytes()
            proof["originals"] = {"slot": snapshot("before-slot.json", slotfile), "lease": snapshot("before-slot.lock.json", slot_lock),
                "dependents": snapshot("before-dependents.json", profile.base / "dependent_processes.json"),
                "component_locks": {name: snapshot("before" + name + ".json", profile.base / name)
                    for name in (".meal-services.lock", ".recipe.lock", ".calendar.lock")}}
            check(CHECKS[1], refused(lambda: reconcile_dependents(ROOT, resource_id=resource_id, slot=0, recover_stale_locks=True)))
            check(CHECKS[2], refused(lambda: recover_orphan_cluster(ROOT, resource_id=resource_id, slot=0)))
            controller_handle.terminate_if_alive()
            child.wait(timeout=10)
            proof["controller_after"] = process_identity(ready["controller"]["pid"])
            check(CHECKS[3], child.returncode is not None and proof["controller_after"]["status"] in ("exited", "not_found"))
            proof["children_after_controller_fault"] = {name: process_identity(s["before"]["pid"]) for name, s in sessions.items()}
            live = any(p["status"] == "alive" and p.get("creation_id") == sessions[name]["before"]["creation_id"]
                for name, p in proof["children_after_controller_fault"].items())
            proof["live_child_reconciliation_refused"] = refused(lambda: reconcile_dependents(ROOT, resource_id=resource_id, slot=0,
                recover_stale_locks=True)) if live else None
            if live and not proof["live_child_reconciliation_refused"]: raise AssertionError("Live original child was not fenced")
            # EOF normally closes stdio servers. If a server retains a PG pool,
            # the diagnostic may close only its pre-attested, still-open handle.
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if all(process_identity(s["before"]["pid"])["status"] in ("not_found", "exited") for s in sessions.values()): break
                time.sleep(0.1)
            proof["diagnostic_child_terminations"] = [name for name, handle in handles.items() if handle.terminate_if_alive()]
            proof["children_after"] = {name: process_identity(s["before"]["pid"]) for name, s in sessions.items()}
            check(CHECKS[4], all(p["status"] in ("not_found", "exited") for p in proof["children_after"].values()))
            check(CHECKS[5], refused(lambda: recover_orphan_cluster(ROOT, resource_id=resource_id, slot=0)))
            check(CHECKS[6], refused(lambda: reconcile_dependents(ROOT, resource_id=resource_id, slot=0)))
            proof["reconciliation"] = reconcile_dependents(ROOT, resource_id=resource_id, slot=0, recover_stale_locks=True)
            check(CHECKS[7], proof["reconciliation"]["outcome"] == "children_confirmed_closed" and slotfile.read_bytes() == original_slot
                and slot_lock.read_bytes() == (ROOT / proof["originals"]["lease"]).read_bytes()
                and any((ROOT / ref).read_bytes() == original_dependents for ref in proof["reconciliation"]["archives"]))
            settled = (profile.base / "dependent_processes.json").read_bytes()
            proof["idempotent"] = reconcile_dependents(ROOT, resource_id=resource_id, slot=0, recover_stale_locks=True)
            check(CHECKS[8], proof["idempotent"]["outcome"] == "already_resolved" and (profile.base / "dependent_processes.json").read_bytes() == settled)
            proof["events_after_reconciliation"] = event_rows(cluster)
            workbook = contained(ROOT, ready["workbook_ref"])
            check(CHECKS[9], proof["events_after_reconciliation"] == ready["events"] and hashlib.sha256(workbook.read_bytes()).hexdigest() == ready["artifact"]["sha256"])
            proof["pg_recovery"] = recover_orphan_cluster(ROOT, resource_id=resource_id, slot=0)
            proof["slot_after_ref"] = snapshot("after-slot.json", slotfile)
            check(CHECKS[10], proof["pg_recovery"]["state"] == "stopped" and not (profile.data / "postmaster.pid").exists()
                and not slot_lock.exists() and (ROOT / proof["pg_recovery"]["lease_archive_ref"]).read_bytes() == (ROOT / proof["originals"]["lease"]).read_bytes())
            proof["shared_after"] = shared_snapshot()
            check(CHECKS[11], proof["shared_before"] == proof["shared_after"])
    finally:
        # Cleanup stays bound to handles opened before fault injection. Unknown
        # inventories are preserved for explicit diagnosis instead of guessed.
        try:
            if controller_handle is not None: controller_handle.terminate_if_alive()
            if child is not None and child.poll() is None:
                if controller_handle is None and os.name == "nt": child.terminate()
                child.wait(timeout=10)
            for handle in handles.values(): handle.terminate_if_alive()
            if ready is not None and set(handles) == {"howtocook", "google_calendar"}:
                reconcile_dependents(ROOT, resource_id=resource_id, slot=0, recover_stale_locks=True)
                if (profile.data / "postmaster.pid").exists(): recover_orphan_cluster(ROOT, resource_id=resource_id, slot=0)
        except Exception as exc: cleanup_errors.append(type(exc).__name__ + ": " + str(exc))
        finally:
            for handle in handles.values(): handle.close()
            if controller_handle is not None: controller_handle.close()
        if profile.base.exists():
            for path in profile.base.rglob("*"):
                if path.is_file() and (path.suffix in (".json", ".txt", ".xlsx", ".mjs") or path.name == "PG_VERSION") and "pgdata" not in path.parts:
                    retain(path)
        for path in private.iterdir():
            if path.is_file(): retain(path)
        save(output / "recovery_observations.json", proof)
        retain(output / "recovery_observations.json")
        result = {"schema_version": "meal-plan-dependent-recovery-diagnostic-v1", "at": datetime.now(timezone.utc).isoformat(),
            "passed": [c["name"] for c in checks] == list(CHECKS) and all(c["passed"] for c in checks) and not cleanup_errors,
            "checks": checks, "profile": profile.record(), "cleanup_errors": cleanup_errors,
            "observations_ref": (output / "recovery_observations.json").relative_to(ROOT).as_posix(), "retained_sha256": retained,
            "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES},
            "execution_kind": "owned_original_mcp_controller_fault_diagnostic", "nemo_second_task_enabled": False,
            "training_eligible": False, "real_model_calls": 0, "training_updates": 0, "reference_solutions_read": False,
            "verified_task_bodies_read": False}
        save(output / "result.json", result)
        print(json.dumps({"passed": result["passed"], "checks": len(checks), "receipt": (output / "result.json").relative_to(ROOT).as_posix()}))
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
