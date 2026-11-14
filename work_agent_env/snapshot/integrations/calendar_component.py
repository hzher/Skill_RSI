"""Pinned original Calendar MCP build and owned episode initialization.

This component does not activate a NeMo task or run an agent loop. Original
preprocess code runs only against an attested episode and its private workspace.
"""
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from uuid import uuid4

from integrations.episode_cluster import EpisodeCluster
from workenv.core import contained, no_links, save
from workenv.leases import owned_lock
from workenv.process_identity import process_identity

PREFIX = "local_servers/Calendar-Autoauth-MCP-Server/"
PREPROCESS = "tasks/finalpool/howtocook-meal-plan-gcal/preprocess/main.py"
MEMBERS = {"src/index.ts": PREFIX + "src/index.ts", "tsconfig.json": PREFIX + "tsconfig.json",
           "original_package.json": PREFIX + "package.json", "preprocess.py": PREPROCESS}
BUILD_INPUTS = ("integrations/calendar_component.py", "scripts/build_calendar_component.py",
                "configs/calendar.package.json", "configs/calendar.package-lock.json", "configs/mainline_sources.lock.json")
COLUMNS = {"id": "text", "summary": "text", "description": "text", "location": "text",
           "start_datetime": "timestamp with time zone", "start_timezone": "text",
           "end_datetime": "timestamp with time zone", "end_timezone": "text", "status": "text",
           "html_link": "text", "creator": "jsonb", "organizer": "jsonb", "attendees": "jsonb",
           "recurrence": "jsonb", "reminders": "jsonb", "created": "timestamp with time zone",
           "updated": "timestamp with time zone"}
OBSERVER = "import fs from 'node:fs';fs.writeFileSync(process.env.WORKENV_CALENDAR_OBSERVATION,JSON.stringify({pid:process.pid}));\n"


def pinned_sources(root):
    root = Path(root).resolve()
    source = json.loads((root / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]["toolathlon_gym"]
    commit = source["commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit): raise ValueError("Invalid original calendar source pin")
    checkout = contained(root, source["checkout"])
    def git(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args], timeout=30,
                                       stderr=subprocess.PIPE, env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
    if git("rev-parse", "HEAD").decode().strip() != commit: raise ValueError("Original calendar checkout pin differs")
    files, objects = {}, {}
    for name, member in MEMBERS.items():
        meta = git("ls-tree", commit, "--", member).decode().strip().split()
        if len(meta) != 4 or meta[:2] != ["100644", "blob"] or meta[3] != member:
            raise ValueError("Missing original calendar public source")
        oid = meta[2]
        if not re.fullmatch(r"[0-9a-f]{40}", oid): raise ValueError("Invalid original source object")
        size = int(git("cat-file", "-s", oid))
        if not 0 < size <= 256 * 1024: raise ValueError("Original calendar source exceeds budget")
        raw = git("cat-file", "blob", oid)
        if len(raw) != size or hashlib.sha1(b"blob " + str(size).encode() + b"\0" + raw).hexdigest() != oid:
            raise ValueError("Original calendar source identity differs")
        files[name], objects[name] = raw, oid
    return commit, files, objects


def build(root, *, install=False):
    root = Path(root).resolve()
    commit, files, objects = pinned_sources(root)
    identity = {"source_commit": commit, "source_objects": objects,
                "source_sha256": {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in BUILD_INPUTS}}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    base = contained(root, ".runtime/calendar-build/" + digest[:24])
    no_links(base, root)
    base.mkdir(parents=True, exist_ok=True)
    with owned_lock(base / ".build.lock", root, "calendar_build"):
        marker = base / "build_owner.json"
        if marker.exists() and json.loads(marker.read_text()) != identity: raise ValueError("Calendar build owner changed")
        save(marker, identity)
        inputs = {**files, "package.json": (root / "configs/calendar.package.json").read_bytes(),
                  "package-lock.json": (root / "configs/calendar.package-lock.json").read_bytes(),
                  "process_observer.mjs": OBSERVER.encode()}
        for name, raw in inputs.items():
            path = contained(base, name)
            no_links(path, root)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if path.read_bytes() != raw: raise ValueError("Existing original calendar build input was changed")
            else:
                with path.open("xb") as stream: stream.write(raw)
        if install:
            npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
            if not npm: raise RuntimeError("npm is required for the explicit locked install")
            subprocess.run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--prefix", str(base),
                            "--registry", "https://registry.npmjs.org", "--fetch-timeout=30000", "--fetch-retries=1"], check=True, timeout=150)
        node = shutil.which("node")
        if not node or not (base / "node_modules/typescript/lib/tsc.js").is_file():
            raise RuntimeError("Node and the locked TypeScript install are required")
        with (base / "tsc.stdout.txt").open("wb") as out, (base / "tsc.stderr.txt").open("wb") as err:
            subprocess.run([str(Path(node).resolve()), str(base / "node_modules/typescript/lib/tsc.js"),
                            "--project", str(base / "tsconfig.json"), "--extendedDiagnostics"], cwd=base,
                           stdout=out, stderr=err, check=True, timeout=60)
        compiled = base / "build/index.js"
        if not compiled.is_file() or compiled.stat().st_size > 256 * 1024: raise ValueError("Original calendar output missing or too large")
        receipt = {"schema_version": "original-calendar-build-v1", "passed": True, **identity,
                   "build_ref": base.relative_to(root).as_posix(), "build_fingerprint": digest,
                   "typecheck_completed": True, "original_sources_modified": False,
                   "retained_sha256": {n: hashlib.sha256((base / n).read_bytes()).hexdigest() for n in (*inputs, "build/index.js")},
                   "runtime_task_activated": False, "real_model_calls": 0, "training_updates": 0}
        save(base / "build_output.json", receipt)
        return receipt


def verify_build(root, receipt):
    root = Path(root).resolve()
    if (receipt.get("schema_version") != "original-calendar-build-v1" or receipt.get("passed") is not True
        or receipt.get("typecheck_completed") is not True or receipt.get("original_sources_modified") is not False):
        raise ValueError("Original calendar has not passed a full build")
    base = contained(root, receipt["build_ref"])
    if not base.is_relative_to(root / ".runtime/calendar-build"): raise ValueError("Calendar escaped its private build root")
    no_links(base, root)
    hashes = receipt.get("source_sha256", {})
    if set(hashes) != set(BUILD_INPUTS): raise ValueError("Calendar build source inventory differs")
    for name, expected in hashes.items():
        path = root / name
        no_links(path, root)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected: raise ValueError("Calendar build source changed")
    identity = {key: receipt[key] for key in ("source_commit", "source_objects", "source_sha256")}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    marker = base / "build_owner.json"
    no_links(marker, root)
    if (receipt.get("build_fingerprint") != digest or base.name != digest[:24]
        or json.loads(marker.read_text(encoding="utf-8")) != identity):
        raise ValueError("Calendar build fingerprint or owner differs")
    retained = receipt.get("retained_sha256", {})
    required = {*MEMBERS, "package.json", "package-lock.json", "process_observer.mjs", "build/index.js"}
    if set(retained) != required: raise ValueError("Calendar retained member inventory differs")
    for name, expected in retained.items():
        path = contained(base, name)
        no_links(path, root)
        if path.stat().st_size > 256 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Original calendar retained bytes changed")
        if name in MEMBERS:
            raw = path.read_bytes()
            if hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != receipt["source_objects"].get(name):
                raise ValueError("Retained calendar source differs from its pinned Git blob")
    emitted = list((base / "build").rglob("*"))
    for path in emitted: no_links(path, root)
    if {p.relative_to(base).as_posix() for p in emitted if p.is_file()} != {"build/index.js"}:
        raise ValueError("Unexpected original calendar compiled module")
    return base


def require_schema(cluster):
    """Fail before the MCP starts if its episode's table contract is absent."""
    conn = cluster.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT column_name,data_type FROM information_schema.columns WHERE table_schema='gcal' AND table_name='events'")
            if dict(cur.fetchall()) != COLUMNS: raise ValueError("Original calendar table contract is missing or differs")
            cur.execute("SELECT count(*) FROM gcal.events")
            return cur.fetchone()[0]
    finally: conn.close()


def initialize(root, receipt, cluster):
    root = Path(root).resolve()
    if not isinstance(cluster, EpisodeCluster) or cluster.profile.owner != root:
        raise ValueError("Calendar episode belongs to a different workspace")
    cluster.require_owner()
    require_schema(cluster)
    base = verify_build(root, receipt)
    workspace = cluster.profile.base / "calendar-workspace"
    no_links(workspace, root)
    workspace.mkdir(exist_ok=True)
    report = workspace / "Weekly_Meal_Plan.xlsx"
    no_links(report, root)  # Original preprocess removes exactly this basename.
    marker = cluster.profile.base / "calendar-initialized.json"
    no_links(marker, root)
    save(marker, {"state": "initializing", "profile": cluster.profile.record()})
    completed = subprocess.run([sys.executable, str(base / "preprocess.py"), "--agent_workspace", str(workspace)],
                               cwd=base, env=cluster.profile.environment(), capture_output=True, timeout=20)
    stem = cluster.profile.base / ("calendar-preprocess-" + uuid4().hex)
    stem.with_suffix(".stdout.txt").write_bytes(completed.stdout)
    stem.with_suffix(".stderr.txt").write_bytes(completed.stderr)
    if completed.returncode or require_schema(cluster) != 0 or report.exists():
        raise RuntimeError("Original calendar initialization failed; no MCP was started")
    save(marker, {"state": "ready", "profile": cluster.profile.record(),
         "build_fingerprint": receipt["build_fingerprint"], "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest()})
    return workspace


@asynccontextmanager
async def calendar_session(root, receipt, cluster, errlog, *, initialization_marker="calendar-initialized.json"):
    """Every process receives this owned episode's two PG environment conventions."""
    root = Path(root).resolve()
    if not isinstance(cluster, EpisodeCluster) or cluster.profile.owner != root:
        raise ValueError("Calendar episode belongs to a different workspace")
    cluster.require_owner()
    require_schema(cluster)
    base = verify_build(root, receipt)
    if initialization_marker not in ("calendar-initialized.json", "order-analysis-initialized.json"):
        raise ValueError("Unsupported task initialization marker")
    marker = cluster.profile.base / initialization_marker
    no_links(marker, root)
    initialized = json.loads(marker.read_text(encoding="utf-8"))
    if (initialized.get("state") != "ready" or initialized.get("profile") != cluster.profile.record()
        or initialized.get("build_fingerprint") != receipt["build_fingerprint"]):
        raise ValueError("Calendar initialization belongs to another episode or build")
    unknown = cluster.profile.base / "calendar-process-unknown.json"
    no_links(unknown, root)
    if unknown.exists(): raise RuntimeError("Previous calendar process shutdown is unknown; preserve this episode")
    node = shutil.which("node")
    if not node: raise RuntimeError("Node is required for the original calendar MCP")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = cluster.profile.environment()
    env.update({"TZ": "UTC", "PGAPPNAME": "workagent-calendar-" + cluster.profile.resource_id})
    with owned_lock(cluster.profile.base / ".calendar.lock", root, "calendar_episode_mcp"):
        observation = cluster.profile.base / ("calendar-process-" + uuid4().hex + ".json")
        env["WORKENV_CALENDAR_OBSERVATION"] = str(observation)
        record = {"profile": cluster.profile.record(), "build_fingerprint": receipt["build_fingerprint"]}
        params = StdioServerParameters(command=str(Path(node).resolve()),
            args=["--import", (base / "process_observer.mjs").as_uri(), str(base / "build/index.js")], env=env, cwd=str(base))
        try:
            async with stdio_client(params, errlog=errlog) as streams:
                async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=15)) as client:
                    await client.initialize()
                    identity = process_identity(json.loads(observation.read_text())["pid"])
                    if identity["status"] != "alive" or not identity.get("creation_id"):
                        raise RuntimeError("Cannot confirm original calendar child identity")
                    record["before"] = identity
                    yield client, record
        finally:
            before = record.get("before")
            if before:
                after = process_identity(before["pid"])
                record["after"] = after
                record["closed"] = after["status"] in ("not_found", "exited") or bool(after.get("creation_id") and after["creation_id"] != before["creation_id"])
                save(observation.with_suffix(".session.json"), record)
                if not record["closed"]:
                    save(unknown, record)
                    raise RuntimeError("Original calendar child shutdown is unknown; episode cannot be reused")
