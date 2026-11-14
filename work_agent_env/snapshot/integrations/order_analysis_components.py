"""Pinned public task sources, owned initialization and complete SQL snapshots."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from adapters.order_analysis_validation import TASK_ID, SOURCE_COMMIT, EVALUATION_SHA256, INSTRUCTION_SHA256
from integrations.calendar_component import require_schema, verify_build
from training.source_api import pinned_blob
from workenv.core import ResourceError, no_links, save

SOURCE_HASHES = {"preprocess/main.py": "17c0472f3a6deb8d8734147da8a37b0016ba948d0ded95697cfed801106b28c7",
                 "evaluation/main.py": EVALUATION_SHA256, "docs/task.md": INSTRUCTION_SHA256,
                 "task_config.json": "f9b4839c5ab62941155fc8876d4a1d8a77100bf4e1cc4f76e18eb5548b094481"}


def public_source(root, member):
    if member not in SOURCE_HASHES: raise ValueError("Only pinned public order-analysis sources are readable")
    source = json.loads((root / "configs/mainline_sources.lock.json").read_text())["sources"]["toolathlon_gym"]
    if source["commit"] != SOURCE_COMMIT: raise ResourceError("Order task source pin changed")
    raw, identity = pinned_blob(root, source, "tasks/finalpool/" + TASK_ID + "/" + member)
    if hashlib.sha256(raw).hexdigest() != SOURCE_HASHES[member]: raise ResourceError("Pinned order task source differs")
    return raw, identity


def snapshot(cluster):
    """Read all owned source rows in a consistent transaction, never references."""
    result = {}
    conn = cluster.connect()
    try:
        conn.rollback()  # connect() attests data_directory in its own read transaction.
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with conn.cursor() as cur:
            for table in ("orders", "products"):
                cur.execute("SELECT count(*) FROM wc." + table)
                count = cur.fetchone()[0]
                if not 0 <= count <= 100000: raise ResourceError("Order source snapshot exceeds row budget")
                cur.execute("SELECT row_to_json(t)::text FROM wc." + table + " t ORDER BY id LIMIT 100001")
                rows = [json.loads(row[0]) for row in cur.fetchall()]
                if len(rows) != count or len({row["id"] for row in rows}) != count: raise ResourceError("Order source snapshot is incomplete")
                result[table] = rows
            require_schema(cluster)
            cur.execute("SELECT id,summary,description,start_datetime,end_datetime FROM gcal.events ORDER BY id LIMIT 1025")
            events = cur.fetchall()
            if len(events) > 1024: raise ResourceError("Calendar snapshot exceeds budget")
            result["events"] = [{"id": row[0], "summary": row[1], "description": row[2],
                "start": {"dateTime": row[3].isoformat()}, "end": {"dateTime": row[4].isoformat()}} for row in events]
    finally: conn.close()
    result["readonly_sha256"] = hashlib.sha256(json.dumps({k:result[k] for k in ("orders", "products")}, sort_keys=True).encode()).hexdigest()
    return result


def initialize(root, cluster, calendar_build, *, resetting=False):
    root = Path(root).resolve()
    cluster.require_owner()
    if cluster.profile.owner != root or not cluster.started: raise ResourceError("Order episode is not owned and active")
    verify_build(root, calendar_build)
    before = snapshot(cluster)
    workspace = cluster.profile.base / "order-analysis-workspace"
    no_links(workspace, root)
    workspace.mkdir(exist_ok=True)
    report = workspace / "Order_Analysis.xlsx"
    no_links(report, root)
    if not resetting and report.exists(): raise ResourceError("Initial order workspace already has an artifact")
    directory = cluster.profile.base / ("order-preprocess-" + uuid4().hex)
    directory.mkdir()
    raw, identity = public_source(root, "preprocess/main.py")
    script = directory / "original_preprocess.py"
    script.write_bytes(raw)
    marker = cluster.profile.base / "order-analysis-initialized.json"
    save(marker, {"state": "initializing", "task_id": TASK_ID, "profile": cluster.profile.record()})
    proc = subprocess.run([sys.executable, str(script), "--agent_workspace", str(workspace)], cwd=directory,
        env=cluster.profile.environment(), capture_output=True, timeout=25)
    (directory / "stdout.txt").write_bytes(proc.stdout)
    (directory / "stderr.txt").write_bytes(proc.stderr)
    after = snapshot(cluster)
    if proc.returncode or after["events"] or after["readonly_sha256"] != before["readonly_sha256"]:
        raise ResourceError("Original order preprocess failed or changed read-only data")
    save(marker, {"state": "ready", "task_id": TASK_ID, "profile": cluster.profile.record(),
        "build_fingerprint": calendar_build["build_fingerprint"], "preprocess_source": identity,
        "preprocess_ref": directory.relative_to(root).as_posix(), "readonly_sha256": before["readonly_sha256"]})
    return after
