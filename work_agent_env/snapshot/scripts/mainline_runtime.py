"""Deploy and operate the real NeMo server and its owned native task resource bridge."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tarfile
import time
import urllib.request
import urllib.error

from nemo_vm import ROOT, VM, ssh_arguments
from provision_nemo_vm import scp
sys.path.insert(0, str(ROOT))

PRIVATE = VM / "runtime-private.json"


def credentials():
    VM.mkdir(parents=True, exist_ok=True)
    if not PRIVATE.exists():
        PRIVATE.write_text(json.dumps({"WORKENV_BRIDGE_TOKEN": secrets.token_urlsafe(48),
                                       "WORKENV_SESSION_SECRET": secrets.token_urlsafe(48)}), encoding="utf-8")
    if os.name != "nt": PRIVATE.chmod(0o600)
    return json.loads(PRIVATE.read_text(encoding="utf-8"))


def remote(script, *, timeout=60):
    return subprocess.run(ssh_arguments() + ["sh -s"], input=script.encode("utf-8"), timeout=timeout, check=True)


def bridge_request(route, *, timeout=190):
    request = urllib.request.Request("http://127.0.0.1:18701" + route, data=b"{}",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + credentials()["WORKENV_BRIDGE_TOKEN"]})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def deploy():
    credentials()
    scp(PRIVATE, "/home/workenv/runtime-private.json")
    scp(ROOT / "integrations/nemo_resources.py", "/home/workenv/nemo_resources.py")
    sync_components()
    launch_nemo()


def sync_components():
    # Only project code/configs, never task answers, run artifacts or private keys.
    bundle = VM / "project-components.tar.gz"
    with tarfile.open(bundle, "w:gz") as target:
        for folder in ("integrations", "training", "evaluation", "configs", "responses_api_agents", "resources_servers"):
            for path in (ROOT / folder).rglob("*"):
                if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
                    if path.suffix not in (".py", ".json", ".yaml", ".txt"): continue
                    target.add(path, arcname="project/" + path.relative_to(ROOT).as_posix(), recursive=False)
    scp(bundle, "/home/workenv/project-components.tar.gz")
    remote("cd /home/workenv\ntar -xzf project-components.tar.gz\n")


def launch_nemo():
    # No shell interpolation of credentials: the Linux launcher reads the private file.
    launcher = VM / "launch_nemo.py"
    launcher.write_text('''import json,os,pathlib,subprocess,sys
root=pathlib.Path('/home/workenv')
private=root/'runtime-private.json'
private.chmod(0o600)
env=os.environ.copy(); env.update(json.loads(private.read_text()))
env['NEMO_GYM_EXTRA_ROOTS']=str(root/'project')
env['PYTHONPATH']=str(root/'project')
pidfile=root/'nemo-server.pid'
if pidfile.exists():
    pid=int(pidfile.read_text())
    try:
        args=pathlib.Path(f'/proc/{pid}/cmdline').read_bytes()
    except FileNotFoundError: args=b''
    if b'/home/workenv/nemo_resources.py' in args: raise RuntimeError('NeMo server already running')
with (root/'nemo-server.stdout.log').open('ab') as out,(root/'nemo-server.stderr.log').open('ab') as err:
    proc=subprocess.Popen([str(root/'nemo-venv/bin/python'),str(root/'nemo_resources.py')],cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True)
pidfile.write_text(str(proc.pid))
print(json.dumps({'nemo_pid':proc.pid,'state':'starting'}))
''', encoding="utf-8")
    scp(launcher, "/home/workenv/launch_nemo.py")
    remote("cd /home/workenv\npython3 launch_nemo.py\n")


def recover_stale_lock():
    from workenv.core import no_links
    from workenv.process_identity import process_identity
    lock = ROOT / ".runtime/native_task.lock"
    no_links(lock, ROOT)
    if not lock.exists(): return
    raw = lock.read_bytes()
    if len(raw) > 8192: raise RuntimeError("Unexpected native lock size")
    value = json.loads(raw)
    if value.get("workspace") != str(ROOT): raise RuntimeError("Native task lock belongs to another workspace")
    identity = process_identity(value["pid"])
    if identity["status"] not in ("not_found", "exited"):
        raise RuntimeError("Native lock owner is alive or unknown; recovery cannot remove its lock")
    if lock.read_bytes() != raw: raise RuntimeError("Native task lock changed during recovery")
    archive = VM / ("recovered-lock-" + str(time.time_ns()) + ".json")
    archive.write_bytes(raw)
    lock.unlink()
    print(json.dumps({"stale_native_lock_recovered": True, "former_pid": value["pid"]}))


def start_bridge(*, recover=False, lease_seconds=900):
    try:
        print(json.dumps(bridge_request("/status", timeout=3)))
        return
    except OSError:
        pass
    if recover: recover_stale_lock()
    elif (ROOT / ".runtime/native_task.lock").exists():
        raise RuntimeError("Owned task lock exists but bridge status is unavailable; no duplicate process will be started")
    subprocess.run([sys.executable, str(ROOT / "scripts/postgres_runtime.py"), "start"], check=True, timeout=60)
    env = os.environ.copy()
    env.update(credentials())
    env["PYTHONIOENCODING"] = "utf-8"
    native_python = ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    command = [str(native_python), "-m", "integrations.toolathlon_bridge", "--lease-seconds", str(lease_seconds)]
    with (VM / "bridge.stdout.log").open("ab") as out, (VM / "bridge.stderr.log").open("ab") as err:
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                start_new_session=os.name != "nt")
    from workenv.process_identity import process_identity
    (VM / "bridge-process.json").write_text(json.dumps({"pid": proc.pid, "command": command, "started_unix": time.time(),
                                                       "process_identity": process_identity(proc.pid)}), encoding="utf-8")
    for _ in range(120):
        if proc.poll() is not None:
            raise RuntimeError("Bridge exited; inspect .runtime/nemo-vm/bridge.stderr.log")
        try:
            print(json.dumps(bridge_request("/status", timeout=3)))
            return
        except OSError:
            time.sleep(.25)
    raise RuntimeError("Resource bridge did not become ready")


def stop_bridge():
    # Close the active original task and release its database lock before stopping PG.
    try:
        print(json.dumps(bridge_request("/shutdown")))
    except urllib.error.URLError as exc:
        if isinstance(exc, urllib.error.HTTPError) or not isinstance(exc.reason, ConnectionRefusedError):
            raise
    for _ in range(40):
        if not (ROOT / ".runtime/native_task.lock").exists():
            break
        time.sleep(.25)
    if (ROOT / ".runtime/native_task.lock").exists():
        raise RuntimeError("Native task lock remains; inspect owned bridge before stopping PG")
    record_path = VM / "bridge-process.json"
    if record_path.exists():
        from workenv.process_identity import process_identity
        record = json.loads(record_path.read_text(encoding="utf-8"))
        for _ in range(40):
            identity = process_identity(record["pid"])
            if identity["status"] in ("not_found", "exited"):
                return
            if identity.get("creation_id") != record["process_identity"].get("creation_id"):
                raise RuntimeError("Recorded bridge PID identity changed; no other process will be stopped")
            time.sleep(.25)
        raise RuntimeError("Bridge released its lock but process exit is not confirmed")


def stop():
    stop_bridge()
    subprocess.run([sys.executable, str(ROOT / "scripts/postgres_runtime.py"), "stop"], check=True, timeout=60)
    remote("""python3 - <<'PY'
from pathlib import Path
import os,signal
p=Path('/home/workenv/nemo-server.pid')
if p.exists():
    pid=int(p.read_text())
    try: command=Path(f'/proc/{pid}/cmdline').read_bytes()
    except FileNotFoundError: command=b''
    if b'/home/workenv/nemo_resources.py' in command: os.kill(pid,signal.SIGTERM)
PY
""")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start-bridge", "stop-bridge", "deploy", "sync-components", "status", "stop"])
    parser.add_argument("--recover-stale-lock", action="store_true")
    parser.add_argument("--lease-seconds", type=int, default=900)
    args = parser.parse_args()
    action = args.action
    if action == "start-bridge": start_bridge(recover=args.recover_stale_lock, lease_seconds=args.lease_seconds)
    elif action == "stop-bridge": stop_bridge()
    elif action == "deploy": deploy()
    elif action == "sync-components": sync_components()
    elif action == "stop": stop()
    else:
        print(json.dumps(bridge_request("/status", timeout=3)))
        with urllib.request.urlopen("http://127.0.0.1:18700/provenance", timeout=20) as response:
            print(json.dumps(json.load(response)))
    return 0


if __name__ == "__main__": raise SystemExit(main())
