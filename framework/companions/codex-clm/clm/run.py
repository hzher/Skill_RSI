"""Launch one native Codex container and its host gateway."""

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

from .gateway import Gateway

ROOT = Path(__file__).resolve().parents[1]
DOCKER = [
    "/mount-data/yangyang-h20-2gpu-train-20261107/env/skill100-20261110/runtime/usr/bin/docker",
    "-H", "unix:///var/docker/proxy/docker-proxy.sock",
]
IMAGE = "sha256:a7e768658111a21f942395b89214267138b74dc6a8f37f91f37772239812e19c"
UID = 1005


def arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, help="Bind a task directory under workspaces/.")
    parser.add_argument("--max-requests", type=int, help="Limit upstream requests for bounded tests.")
    parser.add_argument("--probe", action="store_true", help="Run the container isolation probe.")
    parser.add_argument("codex", nargs=argparse.REMAINDER, help="Codex options after --.")
    args = parser.parse_args()
    if args.codex[:1] == ["--"]:
        args.codex.pop(0)
    return args


def container_options(session, home, state, workspace, bridge, args):
    options = DOCKER + [
        "run", "--rm", "--name", session, "--label", "codex-code-clm.session=" + session,
        "--network", "none", "--user", f"{UID}:{UID}", "--read-only",
        "--cpus", "2", "--memory", "2g", "--pids-limit", "128",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m", "--workdir", "/workspace",
    ]
    if not args.probe:
        options += ["-it"] if sys.stdin.isatty() else ["-i"]
    for key, value in {
        "HOME": "/home/clm",
        "CODEX_HOME": "/home/clm/.codex",
        "CLM_CONTEXT_DIR": "/context",
        "PATH": "/opt/codex/codex-path:/usr/local/bin:/usr/bin:/bin",
    }.items():
        options += ["-e", f"{key}={value}"]
    for source, destination, mode in (
        (ROOT / "clm", "/opt/clm/clm", "ro"),
        (ROOT / "bin/clm-context", "/usr/local/bin/clm-context", "ro"),
        (ROOT / "runtime/codex-0.169.0/x86_64-unknown-linux-musl", "/opt/codex", "ro"),
        (bridge, "/gateway", "ro"),
        (home, "/home/clm", "rw"),
        (state, "/context", "rw"),
        (workspace, "/workspace", "rw"),
    ):
        if ":" in str(source):
            raise ValueError("mount source contains a colon")
        options += ["-v", f"{source}:{destination}:{mode}"]
    return options + [IMAGE]


def terminate(signum, _frame):
    raise SystemExit(128 + signum)


def main():
    args = arguments()
    if args.max_requests is not None and args.max_requests < 1:
        raise ValueError("--max-requests must be positive")
    signal.signal(signal.SIGTERM, terminate)
    os.umask(0o077)
    session = "codex-clm-" + uuid.uuid4().hex
    run = ROOT / "runs" / session
    workspace = run / "work"
    if args.workspace:
        workspace = args.workspace.resolve(strict=True)
        allowed = ROOT / "workspaces"
        if not workspace.is_dir() or not workspace.is_relative_to(allowed) or workspace == allowed:
            raise ValueError("workspace must be a task directory inside " + str(allowed))
    run.mkdir(parents=True)
    home, state = run / "home", run / "context"
    for directory in (home, home / ".codex", state, *((workspace,) if not args.workspace else ())):
        directory.mkdir()
        os.chown(directory, UID, UID)
    marker = "CLM_SESSION_" + session
    print(f"CLM run: {run}", file=sys.stderr, flush=True)
    with tempfile.TemporaryDirectory(prefix="codex-clm-", dir="/tmp") as temporary:
        bridge = Path(temporary) / "gateway"
        bridge.mkdir(mode=0o755)
        bridge.chmod(0o755)
        socket_path = bridge / "responses.sock"
        if args.probe:
            with Gateway(socket_path, state, run / "responses.jsonl",
                         "https://example.invalid", "", marker) as gateway:
                return run_container(gateway, session, home, state, workspace, bridge, args, marker)
        with Gateway(socket_path, state, run / "responses.jsonl", os.environ["OPENAI_BASE_URL"],
                     os.environ["OPENAI_API_KEY"], marker, args.max_requests) as gateway:
            return run_container(gateway, session, home, state, workspace, bridge, args, marker)


def run_container(gateway, session, home, state, workspace, bridge, args, marker):
    (bridge / "responses.sock").chmod(0o666)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    process = None
    try:
        command = container_options(session, home, state, workspace, bridge, args)
        if args.probe:
            command += ["python", "/opt/clm/clm/probe.py"]
        else:
            command += ["python", "/opt/clm/clm/container_entry.py", marker, *args.codex]
        env = {key: value for key, value in os.environ.items() if not key.startswith("OPENAI_")}
        process = subprocess.Popen(command, env=env)
        return process.wait()
    finally:
        gateway.shutdown()
        thread.join()
        subprocess.run([*DOCKER, "rm", "-f", session], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()


if __name__ == "__main__":
    sys.exit(main())
