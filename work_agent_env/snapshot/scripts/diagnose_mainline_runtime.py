"""Read-only local NeMo/resource/PG readiness and observed cold/warm timing."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

from mainline_runtime import ROOT, bridge_request


def main():
    checks, timings = [], []
    started = time.perf_counter()
    try:
        with urllib.request.urlopen("http://127.0.0.1:18700/provenance", timeout=5) as response:
            provenance = json.load(response)
        pinned = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]["nemo_gym"]["commit"]
        valid = provenance.get("framework") == "nemo_gym" and provenance.get("source_commit") == pinned and provenance.get("system") == "Linux"
        checks.append({"name": "actual_nemo_resources", "passed": valid, "provenance": provenance})
    except (OSError, ValueError) as exc:
        checks.append({"name": "actual_nemo_resources", "passed": False, "error_type": type(exc).__name__})
    hot_latency = time.perf_counter() - started
    try:
        status = bridge_request("/status", timeout=3)
        checks.append({"name": "original_task_bridge", "passed": status.get("resource_backend") == "original_toolathlon", "status": status})
    except (OSError, ValueError) as exc:
        checks.append({"name": "original_task_bridge", "passed": False, "error_type": type(exc).__name__})
    sys.path.insert(0, str(ROOT))
    try:
        from adapters.toolathlon_native import database
        with database() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            valid = cursor.fetchone()[0] == 1
        checks.append({"name": "dedicated_postgres_ownership_and_read", "passed": valid})
    except Exception as exc:
        checks.append({"name": "dedicated_postgres_ownership_and_read", "passed": False, "error_type": type(exc).__name__})
    for path in sorted((ROOT / "evidence/commands").glob("*mainline*nemo_schema/command.json"))[-8:]:
        record = json.loads(path.read_text(encoding="utf-8"))
        elapsed = (datetime.fromisoformat(record["ended_at"]) - datetime.fromisoformat(record["started_at"])).total_seconds()
        timings.append({"label": record["label"], "exit_code": record["exit_code"], "wall_seconds": elapsed,
                        "scope": "recorded_schema_import_command_not_a_performance_benchmark"})
    graphics = []
    if os.name == "nt":
        completed = subprocess.run(["powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Json -Compress"],
            capture_output=True, timeout=20)
        if completed.returncode == 0:
            value = json.loads(completed.stdout.decode("utf-8", "replace"))
            graphics = value if isinstance(value, list) else [value]
    result = {"schema_version": "mainline-readonly-runtime-diagnostic-v1", "at": datetime.now(timezone.utc).isoformat(),
              "cpu_environment_ready": all(row["passed"] for row in checks), "checks": checks,
              "single_warm_http_observation_seconds": hot_latency, "historical_import_commands": timings,
              "host_graphics_observed": graphics, "training_ready": False, "model_calls_performed": 0,
              "task_state_reset": False, "services_restarted": False}
    (ROOT / "evidence/mainline/runtime_readiness.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["cpu_environment_ready"] else 1


if __name__ == "__main__": raise SystemExit(main())
