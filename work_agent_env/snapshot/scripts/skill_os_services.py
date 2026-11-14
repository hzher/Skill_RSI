"""Plan/start/stop only project-owned local SkillOS services, with durable receipts."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid
import hashlib
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.benchmark_tasks.contracts import digest
from integrations.benchmark_tasks.store import save


def commands(config):
    result = {}
    for name, role in config["roles"].items():
        port = urlparse(role["base_url"]).port
        if role["backend"] == "vllm":
            command = ["bash", str(ROOT / "scripts/generator_python.sh"), "-m", "vllm.entrypoints.openai.api_server",
                "--model", str(ROOT / role["model_path"]), "--served-model-name", role["served_model"],
                "--host", "127.0.0.1", "--port", str(port), "--dtype", "bfloat16",
                "--generation-config", "vllm", "--max-model-len", str(role["context_limit"]),
                "--max-num-seqs", "1", "--max-num-batched-tokens", "2048", "--enforce-eager",
                "--gpu-memory-utilization", str(role["gpu_memory_utilization"])]
        else:
            command = [str(ROOT / ".runtime/skillos-content-venv/bin/python"), str(ROOT / "scripts/serve_skillos_content.py"),
                "--model", str(ROOT / role["model_path"]), "--served-model", role["served_model"], "--port", str(port)]
        result[name] = {"command": command, "gpus": role["gpus"]}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "start", "status", "stop"))
    parser.add_argument("--config", type=Path, default=ROOT / "configs/skillos_roles.json")
    parser.add_argument("--role", choices=("curator", "selfjudge", "contentjudge"), action="append")
    parser.add_argument("--directory", type=Path, default=ROOT / ".runtime/skillos-services")
    args = parser.parse_args()
    if args.action == "stop":
        # Shutdown must work with system Python even when inference dependencies
        # or the role configuration have changed since the services started.
        receipt_path = args.directory / "receipt.json"
        if not receipt_path.exists():
            print(json.dumps({"services": {}, "message": "No service receipt found"}))
            return 0
        receipt = json.loads(receipt_path.read_text())
        for name in args.role or receipt["services"]:
            current = receipt["services"].get(name)
            if current is None:
                continue
            proc = Path("/proc") / str(current["pid"])
            try:
                owned = proc.stat().st_uid == os.getuid()
                starttime = proc.joinpath("stat").read_text().split(") ", 1)[1].split()[19]
                if not owned or starttime != current["process_starttime"]:
                    print(f"{name}: PID identity differs; no signal sent", file=sys.stderr)
                    continue
                os.killpg(current["pid"], signal.SIGTERM)
            except ProcessLookupError:
                print(f"{name}: recorded PID is not visible", file=sys.stderr)
                continue
            except FileNotFoundError:
                print(f"{name}: recorded PID is not visible", file=sys.stderr)
                continue
            current["stop_requested"] = True
            save(receipt_path, receipt)
            print(f"{name}: SIGTERM sent to process group {current['pid']}", file=sys.stderr)
        print(json.dumps(receipt, ensure_ascii=False))
        return 0
    from integrations.skill_generator.roles import load_roles, SkillOSRoles

    config = load_roles(args.config)
    planned = commands(config)
    if args.action == "plan":
        print(json.dumps(planned, ensure_ascii=False, indent=2))
        return 0
    if args.action == "status":
        async def status():
            roles = SkillOSRoles(config)
            try: return await roles.preflight()
            finally: await roles.close()
        value = asyncio.run(status())
        print(json.dumps(value, ensure_ascii=False))
        return 0 if value["ready"] else 2
    args.directory.mkdir(parents=True, exist_ok=True)
    receipt_path = args.directory / "receipt.json"
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {"config_sha256": digest(config), "services": {}}
    if receipt["config_sha256"] != digest(config):
        raise ValueError("Service ownership directory belongs to a different role configuration")
    for name in args.role or config["roles"]:
        current = receipt["services"].get(name)
        if current is not None:
            proc = Path("/proc") / str(current["pid"])
            alive = proc.exists() and proc.stat().st_uid == os.getuid()
            alive = alive and proc.joinpath("stat").read_text().split(") ", 1)[1].split()[19] == current["process_starttime"]
            if alive:
                continue
        source = ROOT / config["roles"][name]["model_path"] / "deployment_source.json"
        if not source.is_file() or json.loads(source.read_text())["revision"] != config["roles"][name]["model_revision"]:
            raise ValueError("Pinned complete checkpoint unavailable: " + name)
        environment = os.environ.copy()
        environment["CUDA_VISIBLE_DEVICES"] = ",".join(map(str, planned[name]["gpus"]))
        compat = str(ROOT / ".runtime/generator-cuda/compat")
        environment["LD_LIBRARY_PATH"] = compat + (":" + environment["LD_LIBRARY_PATH"] if environment.get("LD_LIBRARY_PATH") else "")
        environment["VLLM_ATTENTION_BACKEND"] = "FLASH_ATTN"
        log_path = args.directory / (name + "_" + uuid.uuid4().hex + ".log")
        with log_path.open("xb") as stream:
            process = subprocess.Popen(planned[name]["command"], cwd=ROOT, env=environment,
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        receipt["services"][name] = {**planned[name], "pid": process.pid, "log_path": str(log_path),
            "model_revision": config["roles"][name]["model_revision"],
            "source_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in
                ("scripts/skill_os_services.py", "scripts/serve_skillos_content.py", "scripts/generator_python.sh",
                 "configs/skillos_roles.json", "configs/skillos-content.requirements.in")},
            "process_starttime": Path(f"/proc/{process.pid}/stat").read_text().split(") ", 1)[1].split()[19]}
        save(receipt_path, receipt)
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
