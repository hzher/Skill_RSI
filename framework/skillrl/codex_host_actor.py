"""Run an account-authenticated Codex CLI on the host that owns the login."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(k + "=" + toml(v) for k, v in value.items()) + "}"
    return json.dumps(value)


def actor_arguments(spec):
    output = Path(spec["output"])
    family = spec.get("family_workspace")
    session = spec.get("resume_thread_id")
    if session and (not family or str(uuid.UUID(session)) != session):
        raise ValueError("Expected a canonical thread UUID and its family workspace")
    workspace = family or str(output / "actor-workspace")
    args = [spec["cli"], "exec", "-C", workspace, "-s", "read-only", "--color", "never"]
    if session:
        args.append("resume")
    args += ["--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--json",
             "-m", spec["model"], "-c", 'approval_policy="never"', "-c", 'model_provider="openai"',
             "-c", 'forced_login_method="chatgpt"', "-c", 'sandbox_mode="read-only"',
             "-c", "model_reasoning_effort=" + json.dumps(spec.get("reasoning_effort", "low")),
             "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"',
             "-c", "features.skip_host_skill_discovery=true"]
    if not family:
        args.append("--ephemeral")
    for feature in ("shell_tool", "multi_agent", "multi_agent_v2", "apps", "plugins", "memories",
                    "skill_search", "browser_use", "browser_use_external", "computer_use",
                    "view_image", "code_mode", "unbounded_connection_retries"):
        args += ["-c", "features." + feature + "=false"]
    args += ["-c", "mcp_servers.benchmark=" + toml(spec["mcp"]), "-o", str(output / "final.txt")]
    return args + ([session] if session else []) + ["-"]


def account_environment():
    # Preserve the existing account/keychain and PATH. Model/provider credentials,
    # endpoint overrides and inherited task capabilities have no place in this run.
    prefixes = ("OPENAI_", "CODEX_", "ANTHROPIC_", "AZURE_OPENAI_", "BENCHMARK_")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(prefixes)}
    if os.environ.get("CODEX_HOME"):
        env["CODEX_HOME"] = os.environ["CODEX_HOME"]
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run(spec):
    path = Path(spec["output"]).resolve()
    path.mkdir(parents=True, exist_ok=True)
    workspace = Path(spec.get("family_workspace") or path / "actor-workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    env = account_environment()
    cli = shutil.which(spec["cli"], path=env.get("PATH"))
    if not cli:
        raise FileNotFoundError("Configured Codex executable was not found on the actor host")
    spec = {**spec, "cli": str(Path(cli).resolve())}
    # Do not persist CLI auth output, which may identify the account or key.
    login = subprocess.run([cli, "-c", 'forced_login_method="chatgpt"', "login", "status"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env=env, timeout=30, cwd=workspace)
    methods = [line.strip().lower() for line in login.stdout.decode(errors="replace").splitlines()]
    if login.returncode or not any(line.startswith("logged in using chatgpt") for line in methods):
        raise RuntimeError("The Codex actor requires an existing ChatGPT login")
    version = subprocess.check_output([cli, "--version"], env=env, text=True, timeout=30).strip()
    with open(cli, "rb") as executable:
        binary_hash = hashlib.file_digest(executable, "sha256").hexdigest()
    args = actor_arguments(spec)
    save(path / "actor-config.json", {"auth_mode": "chatgpt", "model": spec["model"],
         "reasoning_effort": spec.get("reasoning_effort", "low"), "cli_sha256": binary_hash,
         "cli_version": version, "actor_platform": sys.platform, "physical_model_requests": None,
         "capture_scope": "native CLI events and benchmark MCP journal"})
    (path / "prompt.txt").write_text(spec["prompt"], encoding="utf-8")
    started = time.time()
    timed_out = False
    options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
               else {"start_new_session": True})
    with (path / "native-cli.jsonl").open("xb") as out, (path / "native-stderr.txt").open("xb") as err:
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                   cwd=workspace, env=env, **options)
        try:
            process.communicate(spec["prompt"].encode("utf-8"), timeout=spec.get("timeout", 900))
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                killed = subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                                        capture_output=True, timeout=30)
                if killed.returncode and process.poll() is None:
                    raise RuntimeError("Timed-out Codex process tree could not be stopped")
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate(timeout=30)
    receipt = {"exit_code": process.returncode, "timeout": timed_out, "started_at": started,
               "ended_at": time.time(), "wall_seconds": time.time() - started}
    save(path / "actor-exit.json", receipt)
    return 0 if not timed_out and process.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run(json.load(sys.stdin)))
