"""Immutable original email Python sources and a separate hash-locked environment."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from training.email_task_review import PIN, PUBLIC_SHA256, public_blob
from training.source_api import pinned_blob
from workenv.core import contained, no_links, save
from workenv.leases import owned_lock

PREFIX = "local_servers/emails-mcp/"
BUILD_INPUTS = ("integrations/email_build.py", "adapters/email_pg_native.py", "scripts/build_email_component.py",
                "integrations/email_task_identity.py", "integrations/email_components.py", "integrations/inventory_data.py",
                "training/email_task_review.py", "training/inventory_task_review.py", "training/source_api.py",
                "adapters/inventory_alert_validation.py", "adapters/inventory_alert_artifacts.py",
                "adapters/customer_spending_validation.py", "adapters/meal_plan_artifacts.py",
                "integrations/mainline_audit.py", "workenv/leases.py",
                "integrations/episode_cluster.py", "workenv/core.py", "workenv/process_identity.py",
                "configs/email.requirements.in", "configs/email.requirements.lock.txt", "configs/mainline_sources.lock.json")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_inputs(root):
    root = Path(root).resolve()
    source = json.loads((root / BUILD_INPUTS[-1]).read_text())["sources"]["toolathlon_gym"]
    if source["commit"] != PIN: raise ValueError("Email package source pin changed")
    for member in PUBLIC_SHA256: public_blob(root, member)
    checkout = contained(root, source["checkout"])
    raw = subprocess.check_output(["git", "-C", str(checkout), "ls-tree", "-r", "-z", PIN, "--", PREFIX + "src/emails_mcp"], timeout=30)
    if len(raw) > 32768: raise ValueError("Email package tree budget")
    names = []
    for entry in raw.split(b"\0"):
        if not entry: continue
        header, name = entry.decode().split("\t", 1)
        if header.split()[:2] != ["100644", "blob"] or not name.startswith(PREFIX + "src/emails_mcp/") or not name.endswith(".py"):
            raise ValueError("Unexpected email package source member")
        names.append(name)
    if not 1 <= len(names) <= 64: raise ValueError("Email package file inventory budget")
    files, objects = {}, {}
    for name in [*names, PREFIX + "pyproject.toml"]:
        value, identity = pinned_blob(root, source, name)
        if len(value) > 256 * 1024: raise ValueError("Email source file budget")
        if name.endswith(".py"): compile(value, name, "exec")
        files[name.removeprefix(PREFIX)] = value
        objects[name] = identity
    if sum(map(len, files.values())) > 2 * 1024 * 1024: raise ValueError("Email package byte budget")
    return files, objects


def environment_files(base):
    base = Path(base).resolve()
    result = {}
    for path in base.rglob("*"):
        name = path.relative_to(base).as_posix()
        if name == "environment_manifest.json": continue
        if path.is_symlink():
            target = path.resolve()
            if not target.is_relative_to(base): raise ValueError("Email environment link escaped its owner")
            result[name] = {"link": target.relative_to(base).as_posix()}
        elif path.is_file(): result[name] = digest(path)
    if not 10 <= len(result) <= 30000: raise ValueError("Email environment file inventory budget")
    return result


def build(root, *, install=False):
    root = Path(root).resolve()
    if sys.platform != "linux": raise RuntimeError("Email environment installation is currently Linux-only")
    files, objects = source_inputs(root)
    identities = {n: digest(root / n) for n in BUILD_INPUTS}
    identity = {"source_commit": PIN, "source_objects": objects, "source_sha256": identities}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    base = contained(root, ".runtime/email-build/" + fingerprint[:24]); base.mkdir(parents=True, exist_ok=True)
    env_base = contained(root, ".runtime/email-environments/" + identities["configs/email.requirements.lock.txt"][:24])
    env_owner = env_base.parent / (env_base.name + ".owner.json")
    expected_owner = {"workspace": str(root), "lock_sha256": identities["configs/email.requirements.lock.txt"], "python": "3.12"}
    with owned_lock(base / ".build.lock", root, "email_build"):
        marker = base / "build_owner.json"
        if marker.exists() and json.loads(marker.read_text()) != identity: raise ValueError("Email build owner changed")
        save(marker, identity)
        for name, value in files.items():
            path = contained(base, name); path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.read_bytes() != value: raise ValueError("Retained original email source changed")
            if not path.exists():
                with path.open("xb") as stream: stream.write(value)
        if env_base.exists() and (not env_owner.exists() or json.loads(env_owner.read_text()) != expected_owner):
            raise ValueError("Existing email environment is not owned by this lock")
        if install:
            uv = shutil.which("uv")
            if not uv: raise RuntimeError("uv is required for the separate locked email environment")
            if not env_base.exists():
                env_base.parent.mkdir(parents=True, exist_ok=True); save(env_owner, expected_owner)
                subprocess.run([sys.executable, "-m", "venv", "--copies", "--without-pip", str(env_base)], check=True, timeout=60)
            python = env_base / "bin/python"
            subprocess.run([uv, "pip", "sync", "--python", str(python), "--require-hashes",
                            str(root / "configs/email.requirements.lock.txt")], check=True, timeout=240)
        python = env_base / "bin/python"
        if not python.is_file(): raise RuntimeError("Explicit email dependency installation is required")
        metadata = subprocess.check_output([str(python), "-I", "-B", "-c",
            "import sys,json,importlib.metadata as m;print(json.dumps({'python':sys.version,'packages':{d.metadata['Name']:d.version for d in m.distributions()}}))"], timeout=20)
        versions = json.loads(metadata)
        if not versions["python"].startswith("3.12.") or versions["packages"].get("mcp") != "1.13.0":
            raise ValueError("Email Python or MCP SDK version differs")
        manifest = {"owner": expected_owner, **versions, "files": environment_files(env_base)}
        manifest_path = env_base / "environment_manifest.json"
        if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
            raise ValueError("Installed email environment bytes changed")
        save(manifest_path, manifest)
        result = {"schema_version": "original-email-pg-build-v1", "passed": True, **identity,
            "build_ref": base.relative_to(root).as_posix(), "build_fingerprint": fingerprint,
            "environment_ref": env_base.relative_to(root).as_posix(), "environment_manifest_sha256": digest(manifest_path),
            "versions": versions, "syntax_checked": True, "original_sources_modified": False,
            "retained_sha256": {n: digest(base / n) for n in files}, "runtime_task_activated": False,
            "external_messages_sent": 0, "training_updates": 0}
        save(base / "build_output.json", result)
        return result


def verify_build(root, record, *, check_environment=False):
    root = Path(root).resolve()
    if (record.get("schema_version") != "original-email-pg-build-v1" or record.get("passed") is not True
        or record.get("syntax_checked") is not True or record.get("original_sources_modified") is not False
        or record.get("source_commit") != PIN or set(record.get("source_sha256", {})) != set(BUILD_INPUTS)):
        raise ValueError("Email build contract differs")
    if any(digest(root / n) != v for n, v in record["source_sha256"].items()): raise ValueError("Email build inputs changed")
    identity = {k: record[k] for k in ("source_commit", "source_objects", "source_sha256")}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    base = contained(root, record["build_ref"])
    if (not base.is_relative_to(root / ".runtime/email-build") or base.name != fingerprint[:24]
        or record["build_fingerprint"] != fingerprint or json.loads((base / "build_owner.json").read_text()) != identity):
        raise ValueError("Email retained source owner differs")
    expected = {n.removeprefix(PREFIX): obj for n, obj in record["source_objects"].items()}
    if set(record["retained_sha256"]) != set(expected): raise ValueError("Email retained inventory differs")
    for name, checksum in record["retained_sha256"].items():
        path = contained(base, name)
        if digest(path) != checksum or checksum != expected[name]["sha256"]: raise ValueError("Email original source bytes changed")
    if {p.relative_to(base).as_posix() for p in (base / "src").rglob("*") if p.is_file()} != {n for n in expected if n.startswith("src/")}:
        raise ValueError("Unexpected module or bytecode in original email source")
    env_base = contained(root, record["environment_ref"])
    if (not env_base.is_relative_to(root / ".runtime/email-environments")
        or env_base.name != record["source_sha256"]["configs/email.requirements.lock.txt"][:24]
        or digest(env_base / "environment_manifest.json") != record["environment_manifest_sha256"]):
        raise ValueError("Email dependency environment identity differs")
    manifest = json.loads((env_base / "environment_manifest.json").read_text())
    if check_environment and manifest["files"] != environment_files(env_base): raise ValueError("Installed email dependency bytes changed")
    return base, env_base / "bin/python"
