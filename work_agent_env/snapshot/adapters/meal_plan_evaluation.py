"""Run the pinned original meal evaluator only against an owned episode.

The local diagnostic result is not a NeMo task activation or a training reward.
Database, timezone, source and verifier failures retain an unknown task outcome.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

from adapters.meal_plan_validation import ZONE, measure_workbook, result as supplemental_result
from adapters.meal_plan_artifacts import require_no_pending_writes
from integrations.episode_cluster import EpisodeCluster
from integrations.calendar_component import require_schema
from training.recipe_snapshot import verify_snapshot
from training.task_lifecycle_design import pinned_sources, MEMBERS
from workenv.core import no_links, save


def classify_original(exit_code, stdout, stderr):
    if not isinstance(stdout, str) or not isinstance(stderr, str) or len(stdout) + len(stderr) > 256 * 1024:
        return None
    markers = ("=== Checking Google Calendar Events ===", "=== Checking Excel ===", "=== SUMMARY ===")
    if (any(stdout.count(marker) != 1 for marker in markers) or "GCal check error:" in stdout
        or "Traceback (most recent call last)" in stderr or type(exit_code) is not int or exit_code not in (0, 1)):
        return None
    overall = re.findall(r"^\s*Overall: (PASS|FAIL)\s*$", stdout, re.MULTILINE)
    if overall == ["PASS"] and exit_code == 0: return True
    if overall == ["FAIL"] and exit_code == 1: return False
    return None


def combine(upstream_passed, supplemental):
    unknown = type(upstream_passed) is not bool or supplemental.get("evaluation_status") != "completed" or type(supplemental.get("task_passed")) is not bool
    passed = None if unknown else upstream_passed and supplemental["task_passed"]
    return {"schema_version": "original-meal-plan-evaluation-v1", "evaluation_status": "error" if unknown else "completed",
            "task_passed": passed, "upstream_passed": upstream_passed, "supplemental_passed": supplemental.get("task_passed"),
            "error_category": "verifier_infrastructure" if unknown else None, "supplemental": supplemental,
            "reward": None, "score": None, "nemo_task_executed": False, "training_eligible": False}


def event_snapshot(cluster):
    require_schema(cluster)
    conn = cluster.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id,summary,start_datetime,start_timezone,end_datetime,end_timezone FROM gcal.events ORDER BY id LIMIT 129")
            return [{"id": row[0], "summary": row[1], "start": {"dateTime": row[2].isoformat(), "timeZone": row[3]},
                     "end": {"dateTime": row[4].isoformat(), "timeZone": row[5]}} for row in cur.fetchall()]
    finally: conn.close()


def evaluate(root, cluster):
    root = Path(root).resolve()
    if not isinstance(cluster, EpisodeCluster) or cluster.profile.owner != root: raise ValueError("Foreign meal-plan episode")
    cluster.require_owner()
    output = cluster.profile.base / "meal-evaluations" / uuid4().hex
    no_links(output, root)
    output.mkdir(parents=True)
    evaluated = None
    source_identity = None
    source_raw = None
    try:
        marker = cluster.profile.base / "calendar-initialized.json"
        no_links(marker, root)
        initialized = json.loads(marker.read_text(encoding="utf-8"))
        if initialized.get("state") != "ready" or initialized.get("profile") != cluster.profile.record():
            raise ValueError("Meal-plan episode initialization is not ready")
        ZoneInfo(ZONE)
        require_no_pending_writes(cluster)
        snapshot = json.loads((root / "evidence/mainline/howtocook_recipe_snapshot.json").read_text(encoding="utf-8"))
        verify_snapshot(root, snapshot)
        recipes = json.loads((root / snapshot["asset_ref"]).read_text(encoding="utf-8"))
        events = event_snapshot(cluster)
        workspace = cluster.profile.base / "calendar-workspace"
        supplemental = measure_workbook(events, workspace, recipes)
        if supplemental["evaluation_status"] != "completed":
            raise RuntimeError("Supplemental verifier dependencies are unavailable")
        source = json.loads((root / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]["toolathlon_gym"]
        raw, identities = pinned_sources(root, source, members={"evaluation": MEMBERS["evaluation"]})
        source_raw, source_identity = raw["evaluation"], identities["evaluation"]
        script = output / "original_evaluation.py"
        script.write_bytes(source_raw)
        evaluated = subprocess.run([sys.executable, str(script), "--agent_workspace", str(workspace)], cwd=output,
            env=cluster.profile.environment(), capture_output=True, timeout=25)
        (output / "original.stdout.txt").write_bytes(evaluated.stdout)
        (output / "original.stderr.txt").write_bytes(evaluated.stderr)
        original = classify_original(evaluated.returncode, evaluated.stdout.decode("utf-8", "replace"), evaluated.stderr.decode("utf-8", "replace"))
        measured = combine(original, supplemental)
        measured["event_snapshot_sha256"] = hashlib.sha256(json.dumps(events, sort_keys=True).encode()).hexdigest()
        measured["recipe_sha256"] = snapshot["sha256"]
    except Exception as exc:
        # Preserve an unknown outcome even if the original evaluator itself
        # prints Overall: FAIL for a PostgreSQL connection exception.
        measured = combine(None, supplemental_result(None, error=type(exc).__name__, unknown=True))
        measured["infrastructure_error_type"] = type(exc).__name__
    measured.update({"at": datetime.now(timezone.utc).isoformat(), "profile": cluster.profile.record(),
        "original_evaluator_executed": evaluated is not None, "original_evaluator_source": source_identity,
        "upstream_exit_code": evaluated.returncode if evaluated is not None else None,
        "original_evaluator_sha256": hashlib.sha256(source_raw).hexdigest() if source_raw else None,
        "real_model_calls": 0, "training_updates": 0,
        "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in
            ("adapters/meal_plan_evaluation.py", "adapters/meal_plan_validation.py", "adapters/meal_plan_artifacts.py", "integrations/episode_cluster.py",
             "integrations/calendar_component.py", "training/recipe_snapshot.py", "training/task_lifecycle_design.py",
             "configs/mainline_sources.lock.json", "configs/meal_plan.requirements.txt")}})
    target = output / "evaluation.json"
    save(target, measured)
    return measured, target
