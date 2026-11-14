"""Audit retained cold-start stages, process identities, ledgers and real task proofs."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
import re

from integrations.mainline_audit import read_json, source_binding, track_input
from integrations.runtime_identity import BRIDGE_FILES, NEMO_EXTENSIONS, NEMO_FILE, expected_identity
from workenv.core import contained, no_links

LEDGERS = ("resource-sessions.json", "synthetic-sessions.json", "isolated-sessions.json")
COLD_STAGES = ("idle_and_owned_vm", "stop_services", "stop_vm", "confirm_vm_exit", "restart_runtime",
               "ledger_bytes_preserved", "tax_original_task", "meal_original_task", "warm_idempotence", "final_idle")
LINUX_COLD_STAGES = ("idle_and_owned_processes", "stop_services", "confirm_bridge_exit", "confirm_nemo_exit", "restart_runtime",
                     "ledger_bytes_preserved", "tax_original_task", "meal_original_task", "warm_idempotence", "final_idle")
COLD_SOURCES = ("scripts/verify_mainline_startup.py", "scripts/ensure_mainline_runtime.py", "scripts/mainline_runtime.py",
                "scripts/nemo_vm.py", "scripts/linux_mainline.py", "scripts/postgres_runtime.py", "scripts/verify_nemo_toolathlon.py",
                "scripts/verify_nemo_meal_plan.py", "integrations/startup_audit.py", "integrations/mainline_audit.py",
                "integrations/nemo_meal_audit.py", "workenv/process_identity.py", "workenv/leases.py", "workenv/core.py")


def cold_source_identity(root):
    return {**expected_identity(root), **{name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in COLD_SOURCES}}


def task_command_receipt(root, kind, binding, command, output):
    """A saved success from another invocation cannot stand in for this command."""
    if kind not in ("tax", "meal"): raise ValueError("Unknown cold startup task kind")
    actual_ref = output["evidence"] + "/result.json" if kind == "tax" else output["receipt"]
    prefix = "evidence/mainline/nemo-diagnostic-" if kind == "tax" else "evidence/mainline/nemo-meal-plan-"
    if actual_ref != binding["receipt_ref"] or not actual_ref.startswith(prefix) or not actual_ref.endswith("/result.json"):
        raise ValueError("Cold startup task was rebound from its actual command output")
    member = contained(root, actual_ref)
    task = read_json(member, root)
    times = [datetime.fromisoformat(v) for v in (command["started_at"], task["at"], command["finished_at"])]
    if any(t.tzinfo is None for t in times) or not times[0] <= times[1] <= times[2]:
        raise ValueError("Cold startup task proof predates or is outside its command")
    if hashlib.sha256(member.read_bytes()).hexdigest() != binding["sha256"]:
        raise ValueError("Cold startup task receipt changed")
    return member


def audit_cold_startup(root, path):
    from integrations.mainline_audit import audit_diagnostic
    from integrations.nemo_meal_audit import audit_nemo_meal
    root = Path(root).resolve()
    receipt = read_json(path, root)
    native = receipt.get("schema_version") == "mainline-linux-cold-startup-v1"
    stage_names = LINUX_COLD_STAGES if native else COLD_STAGES
    if (receipt.get("schema_version") not in ("mainline-cold-startup-v1", "mainline-linux-cold-startup-v1") or receipt.get("passed") is not True
        or receipt.get("execution_kind") != "owned_idle_cold_restart_and_scripted_original_tasks"
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates", "training_admissions"))
        or any(receipt.get(k) is not False for k in ("automatic_task_retry", "forced_runtime_process_termination", "historical_data_deleted"))):
        raise ValueError("Cold startup scope or completion differs")
    ref = receipt.get("report_ref", "")
    if not re.fullmatch(r"\.runtime/mainline-cold-startup/[0-9a-f]{32}/report\.json", ref):
        raise ValueError("Cold startup report is outside its private attempt")
    original = contained(root, ref)
    if read_json(original, root) != receipt: raise ValueError("Cold startup summary differs from its retained report")
    if receipt["source_sha256"] != cold_source_identity(root): raise ValueError("Cold startup source inventory or bytes changed")
    source_binding(root, receipt["source_sha256"])
    retained = receipt.get("retained_sha256", {})
    if not isinstance(retained, dict) or not 1 <= len(retained) <= 64:
        raise ValueError("Cold startup retained inventory exceeds budget")
    total = 0
    for name, digest in retained.items():
        member = contained(root, name)
        no_links(member, root)
        if member.parent != original.parent or not member.is_file() or member.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Cold startup evidence escaped its bounded attempt")
        total += member.stat().st_size
        if total > 32 * 1024 * 1024 or hashlib.sha256(member.read_bytes()).hexdigest() != digest:
            raise ValueError("Cold startup retained bytes differ or exceed budget")
        track_input(member, root, digest)

    def value(name):
        if name not in retained: raise ValueError("Cold startup raw evidence is not retained")
        return read_json(contained(root, name), root, limit=8 * 1024 * 1024)

    def idle(observed):
        return (observed.get("runtime_ready") is True and "active_session" in observed and observed["active_session"] is None
                and observed.get("active_synthetic_sessions") == [] and observed.get("active_isolated_sessions") == [])

    if not all(idle(receipt[k]) for k in ("before", "after_restart", "final")):
        raise ValueError("Cold startup was not bounded by explicitly idle ready observations")
    expected = expected_identity(root)
    pin = read_json(root / "configs/mainline_sources.lock.json", root)["sources"]["nemo_gym"]["commit"]
    for key in ("before", "after_restart", "final"):
        loaded = receipt[key].get("loaded_source_observation", {})
        if (loaded.get("nemo_commit") != pin or loaded.get("nemo_sha256") != expected[NEMO_FILE]
            or any(loaded.get("nemo_extensions", {}).get(n) != expected[n] for n in NEMO_EXTENSIONS)
            or any(loaded.get("bridge_sha256", {}).get(n) != expected[n] for n in BRIDGE_FILES)):
            raise ValueError("Cold startup ready observation has stale loaded code")
    stages = receipt.get("stages", [])
    if [s.get("name") for s in stages] != list(stage_names) or any(s.get("state") != "completed" for s in stages):
        raise ValueError("Cold startup stage order or completion differs")
    journal_ref = (original.parent / "stages.jsonl").relative_to(root).as_posix()
    if journal_ref not in retained: raise ValueError("Missing incremental cold startup journal")
    journal = [json.loads(line) for line in contained(root, journal_ref).read_text(encoding="utf-8").splitlines()]
    if [(e.get("name"), e.get("state")) for e in journal] != [(name, state) for name in stage_names for state in ("started", "completed")]:
        raise ValueError("Cold startup incremental stage journal differs")
    if journal[1::2] != stages: raise ValueError("Cold startup final stages differ from raw journal")

    if native:
        audit_linux_processes(root, receipt, value, retained)
    for key in (() if native else ("vm_before", "vm_after")):
        observation = receipt[key]
        record, identity = value(observation["record_ref"]), observation["identity"]
        if (record != observation["record"] or retained[observation["record_ref"]] != observation["record_sha256"]
            or identity.get("status") != "alive" or type(identity.get("pid")) is not int or record["pid"] != identity["pid"]
            or not isinstance(identity.get("creation_id"), str) or not re.fullmatch(r"win32-filetime:[0-9]+", identity["creation_id"])
            or "workagent-nemo-v2" not in record.get("command", [])):
            raise ValueError("Cold startup VM ownership/identity differs")
        created = int(identity["creation_id"].split(":", 1)[1]) / 10_000_000 - 11644473600
        if abs(created - record["started_unix"]) > 5: raise ValueError("Cold startup VM PID was reused")
    if not native:
        before_vm, after_vm = receipt["vm_before"]["identity"], receipt["vm_after"]["identity"]
        ended = receipt["vm_exit"]
        if before_vm == after_vm or ended.get("pid") != before_vm["pid"] or ended.get("status") not in ("not_found", "exited"):
            raise ValueError("Cold startup did not prove the old VM exited and a new identity started")
    for label in ("ledgers_before", "ledgers_after_restart"):
        if set(receipt[label]) != set(LEDGERS): raise ValueError("Cold startup ledger inventory differs")
        for name, row in receipt[label].items():
            prefix = "before" if label == "ledgers_before" else "restarted"
            if row["ref"] != (original.parent / (prefix + "-" + name)).relative_to(root).as_posix():
                raise ValueError("Cold startup ledger was rebound to another snapshot")
            if retained.get(row["ref"]) != row["sha256"]: raise ValueError("Cold startup ledger bytes are unbound")
            value(row["ref"])
    if {k: v["sha256"] for k, v in receipt["ledgers_before"].items()} != {k: v["sha256"] for k, v in receipt["ledgers_after_restart"].items()}:
        raise ValueError("Cold restart changed resource ledger bytes")

    commands = {"stop_services": ("mainline_runtime.py", ["stop"]), "stop_vm": ("nemo_vm.py", ["stop"]),
                "restart_runtime": ("ensure_mainline_runtime.py", None), "tax_task": ("verify_nemo_toolathlon.py", []),
                "meal_task": ("verify_nemo_meal_plan.py", []), "warm_idempotence": ("verify_mainline_startup.py", [])}
    if native:
        commands.pop("stop_vm")
        commands.update(stop_services=("linux_mainline.py", ["stop"]), restart_runtime=("linux_mainline.py", ["start"]))
        if type(receipt.get("startup_timeout")) is not int or not 10 <= receipt["startup_timeout"] <= 900:
            raise ValueError("Cold Linux restart is not bounded")
    command_records = {}
    for name, (script, tail) in commands.items():
        row = value((original.parent / (name + ".command.json")).relative_to(root).as_posix())
        command_records[name] = row
        argv = row.get("command", [])
        interpreter = root / (".venv/bin/python" if native else ".runtime/mainline-tools/Scripts/python.exe" if name == "stop_vm" else ".venv/Scripts/python.exe")
        if (type(row.get("exit_code")) is not int or row["exit_code"] != 0 or row.get("error_type") is not None
            or len(argv) < 2 or Path(argv[0]).resolve() != interpreter.resolve() or Path(argv[1]).resolve() != root / "scripts" / script
            or row.get("stdout_ref") not in retained or row.get("stderr_ref") not in retained):
            raise ValueError("Cold startup command or retained output differs")
        if tail is not None and argv[2:] != tail: raise ValueError("Cold startup command arguments differ")
        if tail is None and (len(argv) != 4 or argv[2] != "--timeout" or not argv[3].isdigit() or not 10 <= int(argv[3]) <= 900):
            raise ValueError("Cold startup restart command is not bounded")
        timeout = (receipt["startup_timeout"] + 45 if native and name == "restart_runtime" else
                   int(argv[3]) + 45 if tail is None else {"stop_services": 180, "stop_vm": 30, "tax_task": 300, "meal_task": 300, "warm_idempotence": 90}[name])
        if row.get("timeout_seconds") != timeout: raise ValueError("Cold startup command timeout differs")
    tasks = receipt.get("task_receipts", {})
    if set(tasks) != {"tax", "meal"}: raise ValueError("Cold startup did not verify both original tasks")
    for kind, row in tasks.items():
        command = command_records[kind + "_task"]
        output = value(command["stdout_ref"])
        member = task_command_receipt(root, kind, row, command, output)
        (audit_diagnostic if kind == "tax" else audit_nemo_meal)(root, member)
    warm = value(receipt["warm_receipt_ref"])
    if (warm.get("schema_version") != "mainline-startup-idempotence-v1" or warm.get("passed") is not True
        or warm.get("before") != warm.get("after") or len(warm.get("startup_checks", [])) != 2
        or any(c.get("ready") is not True or c.get("restarted") is not False for c in warm["startup_checks"])):
        raise ValueError("Cold startup warm-idempotence proof differs")
    source_binding(root, warm["source_sha256"])
    if {k: v for k, v in receipt["final"].items() if k not in ("runtime_ready", "loaded_source_observation")} != warm["after"]:
        raise ValueError("Cold startup final process/ledger observation differs from the warm proof")
    return {"passed": True, "stages": len(stages), "original_task_types": 2, "cold_vm_restart": not native, "cold_linux_process_restart": native,
            "ledger_bytes_preserved": True, "training_admissions": 0}


def audit_linux_processes(root, receipt, value, retained):
    """Bind both HTTP PIDs to retained workspace records and Linux creation IDs."""
    for key, state_key in (("runtime_before", "before"), ("runtime_after", "after_restart")):
        runtime = receipt[key]
        for bridge, observed in ((False, runtime), (True, runtime["bridge"])):
            record, identity = value(observed["record_ref"]), observed["identity"]
            if (record != observed["record"] or retained[observed["record_ref"]] != observed["record_sha256"]
                or identity.get("status") != "alive" or type(identity.get("pid")) is not int or identity["pid"] <= 0
                or record.get("pid") != identity["pid"]
                or not re.fullmatch(r"linux-proc:[0-9a-f-]{36}:[0-9]+", identity.get("creation_id", ""))
                or receipt[state_key]["bridge_pid" if bridge else "nemo_pid"] != identity["pid"]):
                raise ValueError("Cold Linux process ownership/identity differs")
            if bridge:
                if (record.get("process_identity") != identity or record.get("command", [])[:3] !=
                    [str(root / ".venv/bin/python"), "-m", "integrations.toolathlon_bridge"]):
                    raise ValueError("Cold Linux bridge record differs")
            elif (record.get("identity") != identity or record.get("workspace") != str(root)
                  or record.get("source") != str(root / "integrations/nemo_resources.py")):
                raise ValueError("Cold Linux NeMo record differs")
    for bridge, ended_key in ((False, "nemo_exit"), (True, "bridge_exit")):
        old, new = receipt["runtime_before"], receipt["runtime_after"]
        if bridge: old, new = old["bridge"], new["bridge"]
        ended = receipt[ended_key]
        if (old["identity"] == new["identity"] or ended.get("pid") != old["identity"]["pid"]
            or ended.get("status") not in ("not_found", "exited")):
            raise ValueError("Cold Linux startup did not prove both old processes exited and new identities started")
