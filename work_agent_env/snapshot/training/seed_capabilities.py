"""Inspect public Gym configuration metadata; never admit or execute a task."""
from collections import Counter
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess

from workenv.core import contained, save
from workenv.handoff import validate_member

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "tasks/finalpool/"
CONFIG_PATH = "training/toolathlon_seed_capabilities.json"
MAX_CONFIG_BYTES = 65536
MAX_CONFIG_TOTAL = 8 * 1024 * 1024
SERVICE_ADAPTERS = {"woocommerce", "google_sheet", "howtocook", "google_calendar"}
COMPLETE_TASK_ADAPTERS = {"wc-sales-tax-summary"}


def object_inventory(raw, expected):
    if not isinstance(raw, bytes) or len(raw) > 4 * 1024 * 1024:
        raise ValueError("Seed tree metadata exceeds its byte budget")
    entries = raw.split(b"\0")
    if len(entries) > 20000: raise ValueError("Seed tree metadata exceeds its entry budget")
    rows = {task: {"preprocess_script_present": False, "evaluation_script_present": False, "file_asset_count": 0} for task in expected}
    for entry in entries:
        if not entry: continue
        header, name = entry.decode("utf-8").split("\t", 1)
        if not name.startswith(PREFIX): raise ValueError("Unexpected seed tree prefix")
        relative = name[len(PREFIX):]
        if "/" not in relative: continue  # Root metadata is not a catalog task.
        task, member = relative.split("/", 1)
        if task not in expected: continue
        validate_member(name)
        mode, kind, blob = header.split()
        row = rows[task]
        if member == "task_config.json":
            if "config_git_blob" in row or mode != "100644" or kind != "blob" or not re.fullmatch("[0-9a-f]{40}", blob):
                raise ValueError("Seed configuration must be one regular pinned Git blob")
            row["config_git_blob"] = blob
        if mode in ("100644", "100755") and kind == "blob":
            if member.startswith("preprocess/") and member.endswith(".py"): row["preprocess_script_present"] = True
            if member.startswith("evaluation/") and member.endswith(".py"): row["evaluation_script_present"] = True
            if member.startswith("files/"): row["file_asset_count"] += 1
    if any("config_git_blob" not in row for row in rows.values()): raise ValueError("Seed configuration inventory is incomplete")
    return rows


def configuration(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_CONFIG_BYTES: raise ValueError("Configuration exceeds its byte budget")
    value = json.loads(raw)
    if not isinstance(value, dict): raise ValueError("Task configuration must be an object")
    result = {}
    for field in ("needed_mcp_servers", "needed_local_tools"):
        names = value.get(field)
        if (not isinstance(names, list) or len(names) > 128
            or any(not isinstance(name, str) or not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) for name in names)):
            raise ValueError("Unsupported service/local-tool metadata declaration")
        result[field] = sorted(set(names))
    return result


def decode_batch(raw, sizes):
    if len(raw) > MAX_CONFIG_TOTAL + len(sizes) * 128: raise ValueError("Git configuration batch exceeds its byte budget")
    stream, result = io.BytesIO(raw), {}
    for oid, size in sizes.items():
        header = stream.readline(128).decode().split()
        if header != [oid, "blob", str(size)]: raise ValueError("Git configuration batch identity changed")
        body = stream.read(size)
        if len(body) != size or stream.read(1) != b"\n": raise ValueError("Truncated Git configuration batch")
        observed = hashlib.sha1(b"blob " + str(size).encode() + b"\0" + body).hexdigest()
        if observed != oid: raise ValueError("Git configuration content differs from its object identity")
        result[oid] = body
    if stream.read(1): raise ValueError("Unexpected trailing Git configuration bytes")
    return result


def local_configuration_blobs(checkout, oids):
    """Never trigger implicit network fetches or read any non-config object."""
    oids = sorted(set(oids))
    if not 1 <= len(oids) <= 1024 or any(not re.fullmatch("[0-9a-f]{40}", oid) for oid in oids):
        raise ValueError("Invalid bounded configuration object request")
    command = ["git", "-C", str(checkout), "cat-file"]
    no_lazy_env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
    requests = ("\n".join(oids) + "\n").encode()
    checked = subprocess.check_output(command + ["--batch-check"], input=requests, timeout=30,
                                     stderr=subprocess.PIPE, env=no_lazy_env)
    lines, sizes = checked.splitlines(), {}
    if len(lines) != len(oids): raise ValueError("Incomplete Git configuration size metadata")
    for oid, line in zip(oids, lines):
        fields = line.decode().split()
        if fields == [oid, "missing"]: continue
        if len(fields) != 3 or fields[:2] != [oid, "blob"]: raise ValueError("Unexpected Git configuration object type")
        size = int(fields[2])
        if not 0 <= size <= MAX_CONFIG_BYTES: raise ValueError("Configuration blob exceeds its individual byte budget")
        sizes[oid] = size
    if sum(sizes.values()) > MAX_CONFIG_TOTAL: raise ValueError("Configuration metadata exceeds its aggregate byte budget")
    if not sizes: return {}
    raw = subprocess.check_output(command + ["--batch"], input=("\n".join(sizes) + "\n").encode(),
                                  timeout=30, stderr=subprocess.PIPE, env=no_lazy_env)
    return decode_batch(raw, sizes)


def summarize(rows):
    services, tools = Counter(), Counter()
    for row in rows.values():
        if row["configuration_status"] == "inspected":
            services.update(row["needed_mcp_servers"])
            tools.update(row["needed_local_tools"])
    return {"task_directories": len(rows), "configurations_inspected": sum(r["configuration_status"] == "inspected" for r in rows.values()),
            "configuration_unknown": sum(r["configuration_status"] != "inspected" for r in rows.values()),
            "declared_service_task_counts": dict(sorted(services.items())), "declared_local_tool_task_counts": dict(sorted(tools.items())),
            "complete_task_adapters": sorted(set(rows) & COMPLETE_TASK_ADAPTERS), "admitted_training_seeds": 0,
            "interpretation": "Service declarations and adapter-name intersection do not establish runnable tasks or training eligibility"}


def fetch_configuration_objects(checkout, source, oids):
    """Explicit bounded fetch of config blob identities only; no checkout updates."""
    oids = sorted(set(oids))
    if not oids: return
    if len(oids) > 1024 or any(not re.fullmatch("[0-9a-f]{40}", oid) for oid in oids):
        raise ValueError("Invalid bounded configuration fetch")
    command = ["git", "-C", str(checkout)]
    remote = subprocess.check_output(command + ["remote", "get-url", "origin"], timeout=10,
                                     env={**os.environ, "GIT_NO_LAZY_FETCH": "1"}).decode().strip()
    if remote.removesuffix(".git").rstrip("/") != source["url"].removesuffix(".git").rstrip("/"):
        raise ValueError("Configuration fetch remote differs from the locked source")
    fetched = subprocess.run(["git", "-c", "fetch.negotiationAlgorithm=noop", "-C", str(checkout), "fetch", "origin",
                              "--no-tags", "--no-write-fetch-head", "--recurse-submodules=no", "--filter=blob:none", "--stdin"],
                             input=("\n".join(oids) + "\n").encode(), capture_output=True, timeout=90)
    if fetched.returncode:
        raise RuntimeError("Pinned configuration fetch failed: " + fetched.stderr.decode("utf-8", errors="replace")[-1000:])


def inspect_capabilities(root=ROOT, *, fetch_missing=False):
    lock = json.loads((root / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))
    catalog = json.loads((root / "training/toolathlon_seed_catalog.json").read_text(encoding="utf-8"))
    source = lock["sources"]["toolathlon_gym"]
    if catalog["commit"] != source["commit"] or not re.fullmatch("[0-9a-f]{40}", source["commit"]):
        raise ValueError("Seed catalog differs from its source pin")
    checkout = contained(root, source["checkout"])
    command = ["git", "-C", str(checkout)]
    no_lazy_env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
    if subprocess.check_output(command + ["rev-parse", "HEAD"], timeout=10, env=no_lazy_env).decode().strip() != source["commit"]:
        raise ValueError("Seed checkout differs from its pin")
    raw = subprocess.check_output(command + ["ls-tree", "-rz", "--full-tree", source["commit"], "tasks/finalpool"],
                                  timeout=30, env=no_lazy_env)
    rows = object_inventory(raw, {row["task_id"] for row in catalog["directories"]})
    blobs = local_configuration_blobs(checkout, [row["config_git_blob"] for row in rows.values()])
    if fetch_missing:
        fetch_configuration_objects(checkout, source, {row["config_git_blob"] for row in rows.values()} - set(blobs))
        blobs = local_configuration_blobs(checkout, [row["config_git_blob"] for row in rows.values()])
    for task, row in rows.items():
        row.update(configuration_status="unknown_missing_local_git_object", complete_task_adapter_available=task in COMPLETE_TASK_ADAPTERS,
                   automatic_training_admission=False)
        body = blobs.get(row["config_git_blob"])
        if body is not None:
            row["config_sha256"] = hashlib.sha256(body).hexdigest()
            try: row.update(configuration(body), configuration_status="inspected")
            except ValueError: row["configuration_status"] = "unknown_unsupported_configuration_metadata"
        if row["configuration_status"] == "inspected":
            row["missing_declared_service_adapters"] = sorted(set(row["needed_mcp_servers"]) - SERVICE_ADAPTERS)
    return {"schema_version": "toolathlon-seed-capabilities-v1", "commit": source["commit"], "source": "toolathlon_gym",
            "summary": summarize(rows), "tasks": dict(sorted(rows.items())), "service_adapter_names": sorted(SERVICE_ADAPTERS),
            "scope": "Public task_config.json service/local-tool names and tree existence only; no prompt or answer files",
            "implicit_network_fetch": False, "reference_solutions_read": False, "verified_task_bodies_read": False,
            "real_model_calls": 0, "training_updates": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-missing", action="store_true", help="Explicitly fetch only missing pinned public configuration blobs; no checkout changes")
    args = parser.parse_args()
    report = inspect_capabilities(fetch_missing=args.fetch_missing)
    save(ROOT / CONFIG_PATH, report)
    paths = ("training/seed_capabilities.py", CONFIG_PATH, "training/toolathlon_seed_catalog.json", "configs/mainline_sources.lock.json", "adapters/toolathlon_native.py")
    receipt = {"schema_version": "seed-capability-inspection-v1", "passed": True, **report["summary"],
               "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths},
               "explicit_config_object_fetch_requested": args.fetch_missing,
               "real_model_calls": 0, "training_updates": 0, "training_admissions": 0}
    save(ROOT / "evidence/mainline/seed_capabilities.json", receipt)
    print(json.dumps(report["summary"]))
    return 0


if __name__ == "__main__": raise SystemExit(main())
