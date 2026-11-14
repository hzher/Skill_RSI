"""Idempotent startup for the prepared, owned Windows/Linux CPU environment."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

from mainline_runtime import ROOT, bridge_request, deploy, start_bridge, stop, stop_bridge
from nemo_vm import VM, ssh_arguments

sys.path.insert(0, str(ROOT))
from integrations.runtime_identity import classify, expected_identity
from workenv.process_identity import process_identity


def observe(expected, pin):
    try:
        with urllib.request.urlopen("http://127.0.0.1:18700/provenance", timeout=3) as response:
            provenance = json.load(response)
    except (OSError, ValueError): provenance = None
    try: bridge = bridge_request("/status", timeout=3)
    except (OSError, ValueError): bridge = None
    return classify(provenance, bridge, expected, pin), provenance, bridge


def guest_probe():
    code = '''from pathlib import Path
import json
root=Path('/home/workenv')
p=root/'nemo-server.pid'
alive=False
if p.exists():
    try: alive=b'/home/workenv/nemo_resources.py' in Path(f'/proc/{int(p.read_text())}/cmdline').read_bytes()
    except FileNotFoundError: pass
print(json.dumps({'nemo_process_alive':alive,'python_installed':(root/'nemo-venv/bin/python').is_file()}))
'''
    try:
        result = subprocess.run(ssh_arguments() + ["python3 -"], input=code.encode(), capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        # Cold TCG boot may accept SSH before it can finish this read-only probe.
        # The outer startup deadline still bounds waiting; no launch is replayed.
        return None
    return json.loads(result.stdout) if result.returncode == 0 else None


def ensure_vm():
    record_path = VM / "process.json"
    if record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        identity = process_identity(record["pid"])
        if identity["status"] == "alive":
            if not identity["creation_id"] or not identity["creation_id"].startswith("win32-filetime:"):
                raise RuntimeError("Cannot confirm owned VM process identity")
            created = int(identity["creation_id"].split(":", 1)[1]) / 10_000_000 - 11644473600
            if abs(created - record["started_unix"]) > 5 or "workagent-nemo-v2" not in record["command"]:
                raise RuntimeError("Recorded VM PID was reused or ownership changed")
            return
        if identity["status"] not in ("not_found", "exited"):
            raise RuntimeError("VM ownership is unknown; no other process will be restarted")
    python = ROOT / ".runtime/mainline-tools/Scripts/python.exe"
    if not python.is_file() or not (VM / "workenv.qcow2").is_file():
        raise RuntimeError("Prepare the VM with setup_nemo_vm.py before starting the environment")
    subprocess.run([str(python), str(ROOT / "scripts/nemo_vm.py"), "start"], check=True, timeout=20)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reload-stale", action="store_true", help="Reload only owned idle services if their loaded source differs")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if not 10 <= args.timeout <= 900: parser.error("timeout must be 10..900 seconds")
    expected = expected_identity(ROOT)
    pin = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]["nemo_gym"]["commit"]
    initial, _, bridge = observe(expected, pin)
    if initial["ready"]:
        print(json.dumps({"ready": True, "restarted": False, "state": initial}))
        return 0
    ensure_vm()
    deadline = time.monotonic() + args.timeout
    launched, reloaded = False, False
    while time.monotonic() < deadline:
        state, provenance, bridge = observe(expected, pin)
        if state["ready"]:
            print(json.dumps({"ready": True, "restarted": reloaded, "launched": launched, "state": state}))
            return 0
        if state["active_session"] is not None:
            raise RuntimeError("An original task is active; finish/close it before reloading adapters")
        stale = bool((provenance and not state["loaded_nemo_source_matches"]) or (bridge and not state["loaded_bridge_source_matches"]))
        if stale:
            if not args.reload_stale: raise RuntimeError("Loaded adapter source differs; use --reload-stale after closing tasks")
            if state["loaded_nemo_source_matches"]:
                # Backend-only changes preserve the validated warm NeMo process.
                stop_bridge()
                start_bridge(recover=True)
                reloaded = True
            else:
                stop()
                start_bridge(recover=True)
                deploy()
                launched, reloaded = True, True
        else:
            if launched:
                # TCG SSH handshakes compete with cold scientific-library imports.
                # Once we launched this process, poll its HTTP readiness directly.
                time.sleep(2)
                continue
            probe = guest_probe()
            if probe is not None:
                if not probe["python_installed"]: raise RuntimeError("Provision the real Linux NeMo package before startup")
                if bridge is None: start_bridge(recover=True)
                if not probe["nemo_process_alive"] and not launched:
                    deploy()
                    launched = True
        time.sleep(2)
    raise TimeoutError("Owned NeMo runtime did not become ready; inspect its retained startup logs")


if __name__ == "__main__": raise SystemExit(main())
