"""Verify warm startup, or explicitly stop/restart an owned idle CPU runtime."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from uuid import uuid4

from mainline_runtime import ROOT, VM, bridge_request
from workenv.core import contained, no_links, save
from workenv.leases import owned_lock
from workenv.process_identity import process_identity


def state():
    with urllib.request.urlopen("http://127.0.0.1:18700/provenance", timeout=5) as response:
        nemo = json.load(response)
    bridge = bridge_request("/status")
    ledger = VM / "resource-sessions.json"
    synthetic_ledger = VM / "synthetic-sessions.json"
    isolated_ledger = VM / "isolated-sessions.json"
    return {"nemo_pid": nemo["pid"], "bridge_pid": bridge["pid"],
            "resource_ledger_sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
            "synthetic_ledger_sha256": hashlib.sha256(synthetic_ledger.read_bytes()).hexdigest() if synthetic_ledger.exists() else None,
            "isolated_ledger_sha256": hashlib.sha256(isolated_ledger.read_bytes()).hexdigest() if isolated_ledger.exists() else None,
            "nemo_loaded_sha256": nemo["loaded_adapter_sha256"], "nemo_loaded_extensions": nemo.get("loaded_extension_sha256"),
            "active_synthetic_sessions": bridge.get("synthetic", {}).get("active_sessions"), "active_isolated_sessions": bridge.get("isolated", {}).get("active_sessions"),
            "active_session": bridge["active_session"]}


def verify_warm():
    before = state()
    calls = []
    for _ in range(2):
        completed = subprocess.run([sys.executable, str(ROOT / "scripts/ensure_mainline_runtime.py")],
                                   cwd=ROOT, capture_output=True, timeout=30, check=True)
        value = json.loads(completed.stdout)
        if not value["ready"] or value["restarted"] is not False:
            raise AssertionError("Warm startup restarted or failed to validate loaded adapters")
        calls.append(value)
    after = state()
    passed = before == after
    result = {"schema_version": "mainline-startup-idempotence-v1", "passed": passed, "before": before, "after": after,
              "startup_checks": calls, "real_model_calls": 0, "training_updates": 0,
              "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
                  "scripts/ensure_mainline_runtime.py", "scripts/verify_mainline_startup.py", "integrations/runtime_identity.py",
                  "integrations/nemo_resources.py", "integrations/nemo_synthetic_resources.py", "integrations/synthetic_backend.py",
                  "integrations/toolathlon_bridge.py")}}
    (ROOT / "evidence/mainline/startup_idempotence.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if passed else 1


def require_idle(observed):
    if (observed.get("runtime_ready") is not True or "active_session" not in observed
        or observed["active_session"] is not None or observed.get("active_synthetic_sessions") != []
        or observed.get("active_isolated_sessions") != []):
        raise RuntimeError("Cold startup requires a ready runtime and all three resource groups explicitly idle")
    return observed


def cold_state():
    from ensure_mainline_runtime import observe
    from integrations.runtime_identity import expected_identity
    pin = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]["nemo_gym"]["commit"]
    classified, provenance, bridge = observe(expected_identity(ROOT), pin)
    return {**state(), "runtime_ready": classified["ready"], "loaded_source_observation": {
        "nemo_commit": (provenance or {}).get("source_commit"),
        "nemo_sha256": (provenance or {}).get("loaded_adapter_sha256"),
        "nemo_extensions": (provenance or {}).get("loaded_extension_sha256"),
        "bridge_sha256": (bridge or {}).get("loaded_source_sha256")}}


def attest_vm():
    from ensure_mainline_runtime import ensure_vm
    marker = VM / "process.json"
    no_links(marker, ROOT)
    if not marker.is_file() or marker.stat().st_size > 32768:
        raise RuntimeError("Cold startup requires the existing owned VM record")
    raw = marker.read_bytes()
    record = json.loads(raw)
    before = process_identity(record["pid"])
    if before["status"] != "alive" or not before.get("creation_id"):
        raise RuntimeError("Cold startup requires the recorded VM to be alive and identified")
    # This call is read-only for the already live VM and checks its creation time/name.
    ensure_vm()
    after = process_identity(record["pid"])
    if marker.read_bytes() != raw or before != after:
        raise RuntimeError("VM identity changed while checking ownership")
    return {"identity": before, "record": record, "record_sha256": hashlib.sha256(raw).hexdigest()}


def attest_linux():
    """Require both live HTTP PIDs to match this workspace's process records."""
    from linux_mainline import _recorded_identity
    owned = _recorded_identity()
    if not owned or owned[1].get("status") != "alive":
        raise RuntimeError("Cold startup requires the owned native NeMo process")
    record, identity = owned
    observed = require_idle(cold_state())
    marker = VM / "bridge-process.json"
    no_links(marker, ROOT)
    if not marker.is_file() or marker.stat().st_size > 32768:
        raise RuntimeError("Missing bounded native bridge ownership record")
    raw = marker.read_bytes()
    bridge = json.loads(raw)
    current = process_identity(bridge["pid"])
    if (current.get("status") != "alive" or current != bridge.get("process_identity")
        or bridge.get("command", [])[:3] != [str(ROOT / ".venv/bin/python"), "-m", "integrations.toolathlon_bridge"]
        or observed["nemo_pid"] != record["pid"] or observed["bridge_pid"] != bridge["pid"]):
        raise RuntimeError("Native runtime HTTP PIDs or process ownership differ")
    raw_nemo = (VM / "nemo-process.json").read_bytes()
    if json.loads(raw_nemo) != record or process_identity(record["pid"]) != identity:
        raise RuntimeError("Native NeMo identity changed during attestation")
    return {"identity": identity, "record": record, "record_sha256": hashlib.sha256(raw_nemo).hexdigest(),
            "bridge": {"identity": current, "record": bridge, "record_sha256": hashlib.sha256(raw).hexdigest()}}


def wait_vm_exit(identity, timeout):
    deadline = time.monotonic() + timeout
    while True:
        observed = process_identity(identity["pid"])
        if observed.get("pid") != identity["pid"]:
            raise RuntimeError("VM exit observation has a different PID")
        if observed["status"] in ("not_found", "exited"):
            return observed
        if observed["status"] != "alive" or observed.get("creation_id") != identity["creation_id"]:
            raise RuntimeError("VM identity became unknown or changed during shutdown")
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned VM did not exit within the shutdown budget; no process was forced to stop")
        time.sleep(.25)


def cold_command(directory, name, command, timeout):
    """Retain the exact attempt, including timeout output; never retry a stage."""
    out, err = directory / (name + ".stdout.txt"), directory / (name + ".stderr.txt")
    record = {"command": command, "timeout_seconds": timeout, "exit_code": None, "error_type": None,
              "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        with out.open("xb") as stdout, err.open("xb") as stderr:
            completed = subprocess.run(command, cwd=ROOT, stdout=stdout, stderr=stderr, timeout=timeout)
            record["exit_code"] = completed.returncode
        if out.stat().st_size + err.stat().st_size > 8 * 1024 * 1024:
            raise RuntimeError("Cold startup command output exceeded its evidence budget")
        if completed.returncode:
            raise RuntimeError("Cold startup command failed: " + name)
    except BaseException as exc:
        record["error_type"] = type(exc).__name__
        raise
    finally:
        record.update(stdout_ref=out.relative_to(ROOT).as_posix(), stderr_ref=err.relative_to(ROOT).as_posix(),
                      finished_at=datetime.now(timezone.utc).isoformat())
        save(directory / (name + ".command.json"), record)
    return record


def verify_cold_task(kind, command):
    from integrations.mainline_audit import audit_diagnostic
    from integrations.nemo_meal_audit import audit_nemo_meal
    stdout = contained(ROOT, command["stdout_ref"])
    if stdout.stat().st_size > 1024 * 1024: raise ValueError("Unexpected task output size")
    result = json.loads(stdout.read_text(encoding="utf-8"))
    reference = result["evidence"] + "/result.json" if kind == "tax" else result["receipt"]
    prefix = "evidence/mainline/nemo-diagnostic-" if kind == "tax" else "evidence/mainline/nemo-meal-plan-"
    if not reference.startswith(prefix) or not reference.endswith("/result.json"):
        raise ValueError("Cold startup task returned an unexpected evidence reference")
    path = contained(ROOT, reference)
    audit = audit_diagnostic(ROOT, path) if kind == "tax" else audit_nemo_meal(ROOT, path)
    if (kind == "tax" and audit.get("passed") is not True) or (kind == "meal" and audit.get("nemo_second_task_enabled") is not True):
        raise RuntimeError("Cold startup task evidence failed its raw audit")
    require_idle(cold_state())
    return {"receipt_ref": reference, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def verify_cold(*, startup_timeout=600, shutdown_timeout=90, native=False):
    from integrations.startup_audit import COLD_STAGES, LINUX_COLD_STAGES, LEDGERS, cold_source_identity
    if native: COLD_STAGES = LINUX_COLD_STAGES
    before_key, after_key, exit_key = ("runtime_before", "runtime_after", "nemo_exit") if native else ("vm_before", "vm_after", "vm_exit")
    directory = ROOT / ".runtime/mainline-cold-startup" / uuid4().hex
    no_links(directory, ROOT)
    directory.mkdir(parents=True)
    report = {"schema_version": "mainline-linux-cold-startup-v1" if native else "mainline-cold-startup-v1", "at": datetime.now(timezone.utc).isoformat(),
              "execution_kind": "owned_idle_cold_restart_and_scripted_original_tasks", "passed": False,
              "report_ref": (directory / "report.json").relative_to(ROOT).as_posix(),
              "stages": [], "source_sha256": cold_source_identity(ROOT), "real_model_calls": 0,
              "training_updates": 0, "training_admissions": 0, "automatic_task_retry": False,
               "forced_runtime_process_termination": False, "historical_data_deleted": False,
               "startup_timeout": startup_timeout, "shutdown_timeout": shutdown_timeout}
    journal = directory / "stages.jsonl"
    journal.touch(exist_ok=False)

    def step(name, operation):
        entry = {"name": name, "state": "started", "started_at": datetime.now(timezone.utc).isoformat()}
        report["stages"].append(entry)
        def persist():
            with journal.open("ab") as stream:
                stream.write((json.dumps(entry) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            save(directory / "progress.json", report)
        persist()
        try:
            value = operation()
            entry.update(state="completed", completed_at=datetime.now(timezone.utc).isoformat())
            return value
        except BaseException as exc:
            entry.update(state="failed", error_type=type(exc).__name__)
            raise
        finally:
            persist()

    def run(name, script, arguments=(), timeout=120, interpreter=None):
        return cold_command(directory, name, [str(interpreter or sys.executable), str(ROOT / "scripts" / script), *arguments], timeout)

    def snapshot_ledgers(label):
        values = {}
        for name in LEDGERS:
            path = VM / name
            no_links(path, ROOT)
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Missing or oversized resource ledger")
            raw = path.read_bytes()
            target = directory / (label + "-" + name)
            with target.open("xb") as stream: stream.write(raw)
            values[name] = {"ref": target.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(raw).hexdigest()}
        return values

    def snapshot_vm(label):
        observed = attest_linux() if native else attest_vm()
        raw = (VM / ("nemo-process.json" if native else "process.json")).read_bytes()
        if hashlib.sha256(raw).hexdigest() != observed["record_sha256"]:
            raise RuntimeError("VM ownership record changed before it was retained")
        target = directory / (label + ("-nemo-process.json" if native else "-vm-process.json"))
        with target.open("xb") as stream: stream.write(raw)
        if native:
            bridge_raw = (VM / "bridge-process.json").read_bytes()
            if hashlib.sha256(bridge_raw).hexdigest() != observed["bridge"]["record_sha256"]:
                raise RuntimeError("Native bridge record changed before retention")
            bridge_target = directory / (label + "-bridge-process.json")
            with bridge_target.open("xb") as stream: stream.write(bridge_raw)
            observed["bridge"]["record_ref"] = bridge_target.relative_to(ROOT).as_posix()
        return {**observed, "record_ref": target.relative_to(ROOT).as_posix()}

    try:
        with owned_lock(VM / ".cold-startup.lock", ROOT, "mainline_cold_startup"):
            def prepare():
                report["before"] = require_idle(cold_state())
                report[before_key] = snapshot_vm("before")
                report["ledgers_before"] = snapshot_ledgers("before")
                require_idle(cold_state())  # Recheck immediately before the first stop.
            step(COLD_STAGES[0], prepare)
            step(COLD_STAGES[1], lambda: run("stop_services", "linux_mainline.py" if native else "mainline_runtime.py", ("stop",), timeout=180))
            if native:
                report["bridge_exit"] = step(COLD_STAGES[2], lambda: wait_vm_exit(report[before_key]["bridge"]["identity"], shutdown_timeout))
            else:
                step(COLD_STAGES[2], lambda: run("stop_vm", "nemo_vm.py", ("stop",), timeout=30,
                                               interpreter=ROOT / ".runtime/mainline-tools/Scripts/python.exe"))
            report[exit_key] = step(COLD_STAGES[3], lambda: wait_vm_exit(report[before_key]["identity"], shutdown_timeout))
            step(COLD_STAGES[4], lambda: run("restart_runtime", "linux_mainline.py" if native else "ensure_mainline_runtime.py",
                                           ("start",) if native else ("--timeout", str(startup_timeout)), timeout=startup_timeout + 45))
            def check_restart():
                report["after_restart"] = require_idle(cold_state())
                report[after_key] = snapshot_vm("restarted")
                if report[before_key]["identity"] == report[after_key]["identity"]:
                    raise RuntimeError("Cold restart did not establish a new process identity")
                if native and report[before_key]["bridge"]["identity"] == report[after_key]["bridge"]["identity"]:
                    raise RuntimeError("Cold restart did not establish a new bridge identity")
                report["ledgers_after_restart"] = snapshot_ledgers("restarted")
                if {k: v["sha256"] for k, v in report["ledgers_before"].items()} != {k: v["sha256"] for k, v in report["ledgers_after_restart"].items()}:
                    raise RuntimeError("Resource ledger bytes changed during the idle stop/restart")
            step(COLD_STAGES[5], check_restart)
            report["task_receipts"] = {}
            for kind, script, stage in (("tax", "verify_nemo_toolathlon.py", COLD_STAGES[6]), ("meal", "verify_nemo_meal_plan.py", COLD_STAGES[7])):
                report["task_receipts"][kind] = step(stage, lambda k=kind, s=script: verify_cold_task(k, run(k + "_task", s, timeout=300)))
            def warm():
                run("warm_idempotence", "verify_mainline_startup.py", timeout=90)
                original = ROOT / "evidence/mainline/startup_idempotence.json"
                no_links(original, ROOT)
                raw = original.read_bytes()
                value = json.loads(raw)
                if value.get("passed") is not True or value["before"] != value["after"]:
                    raise RuntimeError("Warm idempotence failed after cold startup")
                target = directory / "warm_idempotence.json"
                with target.open("xb") as stream: stream.write(raw)
                report["warm_receipt_ref"] = target.relative_to(ROOT).as_posix()
            step(COLD_STAGES[8], warm)
            report["final"] = step(COLD_STAGES[9], lambda: require_idle(cold_state()))
            report["passed"] = True
    except Exception as exc:
        report["passed"] = False
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)[:1000]
    finally:
        # Preserve unknown state; never retry task mutations, force a process, or erase a failed attempt.
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["retained_sha256"] = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in directory.iterdir() if p.is_file() and p.name != "progress.json"}
        save(directory / "progress.json", report)
        save(directory / "report.json", report)
        save(ROOT / "evidence/mainline/cold_startup.json", report)
    print(json.dumps({"passed": report["passed"], "report_ref": report["report_ref"], "error_type": report.get("error_type")}))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cold-restart", action="store_true", help="Explicitly restart the owned idle Linux processes or Windows VM, then verify both tasks")
    parser.add_argument("--startup-timeout", type=int, default=600)
    parser.add_argument("--shutdown-timeout", type=int, default=90)
    args = parser.parse_args()
    if not 10 <= args.startup_timeout <= 900 or not 1 <= args.shutdown_timeout <= 120:
        parser.error("startup timeout must be 10..900 and shutdown timeout 1..120 seconds")
    return verify_cold(startup_timeout=args.startup_timeout, shutdown_timeout=args.shutdown_timeout,
                       native=sys.platform == "linux") if args.cold_restart else verify_warm()


if __name__ == "__main__": raise SystemExit(main())
