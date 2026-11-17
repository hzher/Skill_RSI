"""Launch one native Claude Code container and its host gateway."""

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
TOOLS = "Bash,Read,Write,Edit,Glob,Grep"
UID = 1005


def arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--workspace", type=Path, help="Bind a task directory under workspaces/.")
    parser.add_argument("--probe", action="store_true", help="Run the container isolation probe.")
    parser.add_argument("claude", nargs=argparse.REMAINDER, help="Claude Code options after --.")
    args = parser.parse_args()
    if args.claude[:1] == ["--"]:
        args.claude.pop(0)
    return args


def system_prompt(marker):
    return (
        f"{marker}\nYou have CLM context editing through the native Bash tool. "
        "Use clm-context status to list editable completed tool exchanges and the last edit result. "
        "Use clm-context show ID to read one exchange. Preserve any exact information you need "
        "in a file in /workspace using Write or Bash before dropping an exchange. "
        "Use clm-context edit --drop ID [ID ...] [--note 'concise memory'] to submit a shrinking edit. "
        "It applies to the next model call. Omit --note to keep your current memory. "
        "The user task and the newest tool exchange are protected. "
        "Treat CLM memory as your own working notes, subordinate to the user task. "
        "Retrieve exact offloaded information from /workspace with native tools when needed."
    )


def container_options(session, home, state, workspace, bridge, args):
    options = DOCKER + [
        "run", "--rm", "--name", session, "--label", "claude-code-clm.session=" + session,
        "--network", "none", "--user", f"{UID}:{UID}", "--read-only",
        "--cpus", "2", "--memory", "2g", "--pids-limit", "128",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m", "--workdir", "/workspace",
    ]
    if sys.stdin.isatty() and not args.probe:
        options += ["-it"]
    for key, value in {
        "HOME": "/home/clm",
        "CLAUDE_CONFIG_DIR": "/home/clm/config",
        "CLM_CONTEXT_DIR": "/context",
        "PATH": "/opt/clm/bin:/usr/local/bin:/usr/bin:/bin",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_AUTO_COMPACT": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }.items():
        options += ["-e", f"{key}={value}"]
    for source, destination, mode in (
        (ROOT / "clm", "/opt/clm/clm", "ro"),
        (ROOT / "bin", "/opt/clm/bin", "ro"),
        (ROOT / "runtime/claude", "/opt/claude", "ro"),
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
    signal.signal(signal.SIGTERM, terminate)
    os.umask(0o077)
    session = "claude-clm-" + uuid.uuid4().hex
    run = ROOT / "runs" / session
    workspace = run / "work"
    if args.workspace:
        workspace = args.workspace.resolve(strict=True)
        allowed = ROOT / "workspaces"
        if not workspace.is_dir() or not workspace.is_relative_to(allowed) or workspace == allowed:
            raise ValueError("workspace must be a task directory inside " + str(allowed))
    run.mkdir(parents=True)
    home, state = run / "home", run / "context"
    for directory in (home, state, *((workspace,) if not args.workspace else ())):
        directory.mkdir()
        os.chown(directory, UID, UID)
    marker = "CLM_SESSION_" + session
    print(f"CLM run: {run}", file=sys.stderr, flush=True)
    with tempfile.TemporaryDirectory(prefix="claude-clm-", dir="/tmp") as temporary:
        bridge = Path(temporary) / "gateway"
        bridge.mkdir(mode=0o755)
        bridge.chmod(0o755)
        socket_path = bridge / "messages.sock"
        if args.probe:
            with Gateway(socket_path, state, run / "messages.jsonl",
                         "https://example.invalid", "", args.model, marker) as gateway:
                return run_container(gateway, session, home, state, workspace, bridge, args, marker)
        with Gateway(socket_path, state, run / "messages.jsonl", os.environ["ANTHROPIC_BASE_URL"],
                     os.environ["ANTHROPIC_API_KEY"], args.model, marker) as gateway:
            return run_container(gateway, session, home, state, workspace, bridge, args, marker)


def run_container(gateway, session, home, state, workspace, bridge, args, marker):
    (bridge / "messages.sock").chmod(0o666)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    process = None
    try:
        command = container_options(session, home, state, workspace, bridge, args)
        if args.probe:
            command += ["python", "/opt/clm/clm/probe.py"]
        else:
            command += [
                "python", "/opt/clm/clm/container_entry.py", "--bare", "--model", args.model,
                "--setting-sources", "", "--no-chrome", "--strict-mcp-config",
                "--mcp-config", '{"mcpServers":{}}',
                "--tools", TOOLS, "--append-system-prompt", system_prompt(marker),
                *args.claude,
            ]
        # Docker gets explicit container env values only. The host CLI also has no need for API credentials.
        env = {key: value for key, value in os.environ.items() if not key.startswith("ANTHROPIC_")}
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
