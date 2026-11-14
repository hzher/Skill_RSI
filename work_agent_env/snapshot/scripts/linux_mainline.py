"""Operate the owned NeMo and business bridge directly on a Linux host.

This entrypoint never installs system packages or stops a process whose saved
creation identity differs from the live process.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mainline_runtime import PRIVATE, VM, bridge_request, credentials, start_bridge, stop_bridge
from workenv.postgres_binary import postgres_binary
from workenv.process_identity import process_identity
from integrations.runtime_identity import classify, expected_identity

PROCESS = VM / "nemo-process.json"
NEMO_SOURCE = ROOT / "integrations/nemo_resources.py"
NEMO_PYTHON = ROOT / ".runtime/nemo-venv/bin/python"
HOST_PYTHON = ROOT / ".venv/bin/python"


def _version(python):
    return subprocess.check_output([str(python), "-c", "import platform; print(platform.python_version())"],
                                   text=True, timeout=15).strip()


def preflight():
    if sys.platform != "linux":
        raise RuntimeError("This entrypoint requires Linux")
    if not HOST_PYTHON.is_file() or not NEMO_PYTHON.is_file():
        raise RuntimeError("Install .venv (Python 3.12) and .runtime/nemo-venv (Python 3.13.14) first")
    host_version, nemo_version = _version(HOST_PYTHON), _version(NEMO_PYTHON)
    if host_version != "3.12.11" or nemo_version != "3.13.14":
        raise RuntimeError("Interpreter versions differ from the pinned runtime")
    lock = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]
    source = ROOT / lock["nemo_gym"]["checkout"]
    if not source.is_dir():
        raise RuntimeError("Pinned NeMo source checkout is missing")
    head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True, timeout=10).strip()
    if head != lock["nemo_gym"]["commit"]:
        raise RuntimeError("NeMo checkout differs from the pinned commit")
    if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"],
                               text=True, timeout=10).strip():
        raise RuntimeError("NeMo checkout has tracked changes")
    task_source = ROOT / lock["toolathlon_gym"]["checkout"]
    if not task_source.is_dir():
        raise RuntimeError("Pinned Toolathlon-GYM checkout is missing")
    task_head = subprocess.check_output(["git", "-C", str(task_source), "rev-parse", "HEAD"],
                                        text=True, timeout=10).strip()
    if task_head != lock["toolathlon_gym"]["commit"]:
        raise RuntimeError("Toolathlon-GYM checkout differs from the pinned commit")
    if subprocess.check_output(["git", "-C", str(task_source), "status", "--porcelain", "--untracked-files=no"],
                               text=True, timeout=10).strip():
        raise RuntimeError("Toolathlon-GYM checkout has tracked changes")
    pg_versions = {}
    for name in ("initdb", "pg_ctl", "psql", "createdb", "pg_dump"):
        binary = postgres_binary(ROOT, name)
        version = subprocess.check_output([str(binary), "--version"], text=True, timeout=10).strip()
        if " 15." not in version:
            raise RuntimeError(f"Original task databases require PostgreSQL 15 {name}: {binary}")
        pg_versions[name] = version
    if not shutil.which("node") or not shutil.which("npm"):
        raise RuntimeError("Node and npm are required for the original MCP components")
    for path in (ROOT / "evidence/woocommerce_build.json",
                 ROOT / "evidence/mainline/howtocook_build.json",
                 ROOT / "evidence/mainline/calendar_build.json",
                 ROOT / ".runtime/database_initialized.json",
                 ROOT / ".runtime/nemo-vm/isolated-seed.json",
                 ROOT / ".runtime/nemo-vm/meal-plan-seed.json",
                 ROOT / ".runtime/nemo-vm/order-analysis-seed.json",
                 ROOT / ".runtime/nemo-vm/fulfillment-seed.json", ROOT / ".runtime/nemo-vm/shipping-seed.json"):
        if not path.is_file():
            raise RuntimeError(f"Required original-service preparation is missing: {path.relative_to(ROOT)}")
    return {"host_python": host_version, "nemo_python": nemo_version,
            "nemo_commit": head, "postgres_bin": str(postgres_binary(ROOT, "pg_ctl")),
            "postgres_versions": pg_versions,
            "runtime_dir": str(VM)}


def _recorded_identity():
    if not PROCESS.exists():
        return None
    record = json.loads(PROCESS.read_text(encoding="utf-8"))
    if record.get("source") != str(NEMO_SOURCE) or record.get("workspace") != str(ROOT):
        raise RuntimeError("NeMo process record belongs to another source or workspace")
    current = process_identity(record["pid"])
    if current["status"] == "alive" and current != record["identity"]:
        raise RuntimeError("Recorded NeMo PID was reused or changed identity")
    if current["status"] == "unknown":
        raise RuntimeError("NeMo process identity is unknown")
    return record, current


def _provenance():
    with urllib.request.urlopen("http://127.0.0.1:18700/provenance", timeout=3) as response:
        return json.load(response)


def _ready(provenance, bridge):
    pin = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]["nemo_gym"]["commit"]
    return classify(provenance, bridge, expected_identity(ROOT), pin)


def start():
    preflight()
    observed = _recorded_identity()
    if observed and observed[1]["status"] == "alive":
        provenance = _provenance()
        bridge = bridge_request("/status", timeout=3)
        if provenance.get("pid") != observed[0]["pid"] or not _ready(provenance, bridge)["ready"]:
            raise RuntimeError("Live NeMo provenance differs from the owned process")
        print(json.dumps({"ready": True, "reused": True, "provenance": provenance}))
        return
    VM.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        # A just-stopped loopback listener can leave TIME_WAIT connections.
        # Keep rejecting a live listener while allowing an owned cold restart.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 18700))
    start_bridge(recover=True)
    private = credentials()
    shutil.copyfile(NEMO_SOURCE, VM / "nemo_resources.py")
    environment = os.environ.copy()
    environment.update(private)
    environment.update({"WORKENV_BRIDGE_URL": "http://127.0.0.1:18701",
                        "WORKENV_NEMO_HOST": "127.0.0.1",
                        "NEMO_GYM_EXTRA_ROOTS": str(ROOT), "PYTHONPATH": str(ROOT),
                        "PYTHONIOENCODING": "utf-8"})
    with (VM / "nemo.stdout.log").open("ab") as stdout, (VM / "nemo.stderr.log").open("ab") as stderr:
        child = subprocess.Popen([str(NEMO_PYTHON), str(VM / "nemo_resources.py")], cwd=VM, env=environment,
                                 stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
    identity = process_identity(child.pid)
    if identity["status"] != "alive" or not identity["creation_id"]:
        raise RuntimeError("NeMo process started without an attested Linux creation identity")
    PROCESS.write_text(json.dumps({"workspace": str(ROOT), "source": str(NEMO_SOURCE),
                                   "pid": child.pid, "identity": identity}, indent=2) + "\n", encoding="utf-8")
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("NeMo exited; inspect .runtime/nemo-vm/nemo.stderr.log")
        try:
            provenance = _provenance()
        except OSError:
            time.sleep(2)
            continue
        bridge = bridge_request("/status", timeout=3)
        if provenance.get("pid") != child.pid or not _ready(provenance, bridge)["ready"]:
            raise RuntimeError("NeMo provenance did not match the launched process and source")
        print(json.dumps({"ready": True, "reused": False, "provenance": provenance}))
        return
    raise RuntimeError("NeMo startup timed out; retain logs and process record for inspection")


def status():
    observed = _recorded_identity()
    result = {"process": observed[1] if observed else None, "nemo": None, "bridge": None}
    try: result["nemo"] = _provenance()
    except OSError: pass
    if PRIVATE.is_file():
        try: result["bridge"] = bridge_request("/status", timeout=3)
        except OSError: pass
    result["identity"] = _ready(result["nemo"], result["bridge"])
    result["owned_process"] = bool(observed and observed[1]["status"] == "alive"
                                   and result["nemo"] and result["nemo"].get("pid") == observed[0]["pid"])
    print(json.dumps(result))
    return 0 if result["identity"]["ready"] and result["owned_process"] else 1


def stop():
    # The bridge closes its owned sessions and PostgreSQL cluster first.
    stop_bridge()
    subprocess.run([sys.executable, str(ROOT / "scripts/postgres_runtime.py"), "stop"], check=True, timeout=60)
    observed = _recorded_identity()
    if not observed or observed[1]["status"] in ("not_found", "exited"):
        print(json.dumps({"stopped": True, "nemo_already_exited": True}))
        return
    record, current = observed
    if current != record["identity"]:
        raise RuntimeError("NeMo PID identity changed; no signal sent")
    os.kill(record["pid"], signal.SIGTERM)
    for _ in range(100):
        state = process_identity(record["pid"])
        if state["status"] in ("not_found", "exited") or state["creation_id"] != current["creation_id"]:
            print(json.dumps({"stopped": True, "pid": record["pid"]}))
            return
        time.sleep(.1)
    raise RuntimeError("NeMo exit is unconfirmed; retain its process record")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "start", "status", "stop"))
    action = parser.parse_args().action
    if action == "preflight": print(json.dumps(preflight()))
    elif action == "start": start()
    elif action == "status": return status()
    else: stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
