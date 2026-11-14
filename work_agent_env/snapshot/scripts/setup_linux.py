"""Prepare pinned project dependencies for the direct Linux mainline runtime.

Run individual stages in order; an existing checkout or environment with a
different identity is never replaced automatically.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workenv.postgres_binary import postgres_binary

LOCK = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]
PG_VERSION = "15.19"
PG_SHA256 = "4bf5474f0ee4afc55de5e71fb5cc8cd133ee56cd1bf16092093897b7c93328fb"
PG_URL = f"https://ftp.postgresql.org/pub/source/v{PG_VERSION}/postgresql-{PG_VERSION}.tar.gz"
PG_TOOLS = ("initdb", "pg_ctl", "psql", "createdb", "pg_dump")


def run(*argv, timeout=1800, cwd=None):
    subprocess.run([str(value) for value in argv], cwd=cwd or ROOT, check=True, timeout=timeout)


def checkout(name, *, sparse=False):
    source = LOCK[name]
    target = ROOT / source["checkout"]
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        run("git", "init", target)
        run("git", "-C", target, "remote", "add", "origin", source["url"])
    if not (target / ".git").is_dir():
        raise RuntimeError(f"{name} target exists but is not a Git checkout: {target}")
    origin = subprocess.check_output(["git", "-C", str(target), "remote", "get-url", "origin"],
                                     text=True, timeout=10).strip()
    if origin != source["url"]:
        raise RuntimeError(f"{name} checkout origin differs from its pin")
    existing = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
    if existing.returncode:
        # A previous download may have stopped after git init; resume the same pin.
        if sparse:
            run("git", "-C", target, "sparse-checkout", "set", "--no-cone",
                "/README.md", "/AGENTS.md", "/LICENSE", "/pyproject.toml", "/Dockerfile",
                "/scripts/", "/utils/", "/configs/", "/db/init.sql.gz", "/local_servers/woocommerce-mcp/",
                "/local_servers/mcp-google-sheets/", "/local_servers/HowToCook-mcp/",
                "/local_servers/Calendar-Autoauth-MCP-Server/",
                "/tasks/finalpool/wc-sales-tax-summary/", "/tasks/finalpool/howtocook-meal-plan-gcal/",
                "/tasks/finalpool/wc-order-analysis-gcal/")
        # Match the bounded first-install path verified on local WSL. HTTP/1.1
        # avoids interrupted HTTP/2 transfers; sparse checkouts fetch blobs only
        # when their selected public files are needed.
        run("git", "-c", "http.version=HTTP/1.1", "-C", target, "fetch", "--filter=blob:none", "--depth", "1", "origin", source["commit"])
        run("git", "-C", target, "checkout", "--detach", source["commit"])
    head = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(target), "status", "--porcelain", "--untracked-files=no"], text=True).strip()
    if head != source["commit"] or dirty:
        raise RuntimeError(f"{name} checkout differs from its pin or has tracked changes")
    print(json.dumps({"source": name, "commit": head, "checkout": str(target)}))


def python_environments():
    uv = shutil.which("uv")
    if not uv: raise RuntimeError("Install uv before creating the pinned Python environments")
    for version, environment in (("3.12.11", ROOT / ".venv"),
                                 ("3.13.14", ROOT / ".runtime/nemo-venv")):
        if not environment.exists():
            run(uv, "python", "install", version)
            run(uv, "venv", "--python", version, environment)
        python = environment / "bin/python"
        if not python.is_file(): raise RuntimeError(f"Incomplete Python environment: {environment}")
        actual = subprocess.check_output([str(python), "-c", "import platform; print(platform.python_version())"],
                                         text=True).strip()
        if actual != version: raise RuntimeError(f"Existing {environment} has Python {actual}, expected {version}")
    run(uv, "pip", "sync", "--python", ROOT / ".venv/bin/python", ROOT / "configs/requirements.lock")
    run(uv, "pip", "install", "--python", ROOT / ".runtime/nemo-venv/bin/python",
        "-e", ROOT / ".upstream/nemo-gym", "-r", ROOT / "configs/nemo_linux_runtime.requirements.txt")
    run(uv, "pip", "check", "--python", ROOT / ".venv/bin/python")
    run(uv, "pip", "check", "--python", ROOT / ".runtime/nemo-venv/bin/python")


def postgresql():
    # A system or user-supplied PostgreSQL 15 is equally acceptable.
    try:
        versions = [subprocess.check_output([str(postgres_binary(ROOT, name)), "--version"],
                                          text=True, timeout=10).strip() for name in PG_TOOLS]
        if all(" 15." in value for value in versions):
            print(json.dumps({"postgresql": "reused", "versions": versions}))
            return
    except (RuntimeError, OSError, subprocess.SubprocessError):
        pass
    if not shutil.which("gcc") or not shutil.which("make"):
        raise RuntimeError("Building PostgreSQL without sudo requires gcc and make")
    runtime = ROOT / ".runtime"
    sources = runtime / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    archive = sources / f"postgresql-{PG_VERSION}.tar.gz"
    if not archive.exists():
        temporary = sources / f"postgresql-{PG_VERSION}.tar.gz.download"
        if temporary.exists():
            raise RuntimeError(f"Incomplete PostgreSQL download retained at {temporary}")
        with urllib.request.urlopen(PG_URL, timeout=60) as response, temporary.open("xb") as output:
            shutil.copyfileobj(response, output)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != PG_SHA256:
            raise RuntimeError(f"PostgreSQL download digest mismatch; retained at {temporary}")
        temporary.rename(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != PG_SHA256:
        raise RuntimeError(f"PostgreSQL archive digest mismatch: {archive}")
    source = sources / f"postgresql-{PG_VERSION}"
    if not source.exists():
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                path = Path(member.name)
                if not path.parts or path.parts[0] != source.name or path.is_absolute() or ".." in path.parts:
                    raise RuntimeError("PostgreSQL archive contains a path outside its source directory")
            bundle.extractall(sources)
    if not (source / "configure").is_file():
        raise RuntimeError(f"Incomplete PostgreSQL source extraction retained at {source}")
    target = runtime / "pgsql"
    marker = target / "workagent-build-owner.json"
    if target.exists() and any(target.iterdir()) and not marker.is_file():
        raise RuntimeError(f"Existing PostgreSQL install is unowned: {target}")
    target.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text(json.dumps({"workspace": str(ROOT), "version": PG_VERSION,
                                      "archive_sha256": PG_SHA256}) + "\n", encoding="utf-8")
    elif json.loads(marker.read_text(encoding="utf-8")) != {
            "workspace": str(ROOT), "version": PG_VERSION, "archive_sha256": PG_SHA256}:
        raise RuntimeError("PostgreSQL build owner differs from the requested source")
    build = sources / f"postgresql-{PG_VERSION}-build"
    build.mkdir(exist_ok=True)
    if not (build / "Makefile").exists():
        run(source / "configure", f"--prefix={target}", "--without-readline",
            timeout=300, cwd=build)
    run("make", "-C", build, "-j4", timeout=1800)
    run("make", "-C", build, "install", timeout=600)
    for name in PG_TOOLS:
        binary = target / "bin" / name
        version = subprocess.check_output([str(binary), "--version"], text=True, timeout=10).strip()
        if f" {PG_VERSION}" not in version:
            raise RuntimeError(f"Installed PostgreSQL {name} has an unexpected version: {version}")
    print(json.dumps({"postgresql": "built", "version": PG_VERSION, "prefix": str(target)}))


def components():
    python = ROOT / ".venv/bin/python"
    run(python, ROOT / "scripts/build_woocommerce_native.py")
    run(python, "-m", "training.recipe_snapshot")
    run(python, ROOT / "scripts/build_howtocook_offline.py", "--install")
    run(python, ROOT / "scripts/build_calendar_component.py", "--install")


def database():
    postgres = postgres_binary(ROOT, "pg_ctl")
    version = subprocess.check_output([str(postgres), "--version"], text=True).strip()
    if " 15." not in version: raise RuntimeError("Pinned task schemas require PostgreSQL major version 15")
    python = ROOT / ".venv/bin/python"
    run(python, ROOT / "scripts/postgres_runtime.py", "start")
    run(python, ROOT / "scripts/postgres_runtime.py", "init-db")
    run(python, ROOT / "scripts/prepare_episode_seed.py")
    run(python, ROOT / "scripts/prepare_episode_seed.py", "--task-id", "howtocook-meal-plan-gcal")
    run(python, ROOT / "scripts/prepare_episode_seed.py", "--task-id", "wc-order-analysis-gcal")
    run(python, ROOT / "scripts/prepare_episode_seed.py", "--task-id", "wc-order-fulfillment-tracker")
    run(python, ROOT / "scripts/prepare_episode_seed.py", "--task-id", "wc-shipping-zone-gsheet")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("sources", "python", "postgres", "components", "database", "all"))
    stage = parser.parse_args().stage
    if sys.platform != "linux": raise RuntimeError("Use the existing Windows setup on non-Linux hosts")
    if stage in ("sources", "all"):
        checkout("toolathlon_gym", sparse=True)
        checkout("nemo_gym")
    if stage in ("python", "all"): python_environments()
    if stage in ("postgres", "all"): postgresql()
    if stage in ("components", "all"): components()
    if stage in ("database", "all"): database()
    return 0


if __name__ == "__main__": raise SystemExit(main())
