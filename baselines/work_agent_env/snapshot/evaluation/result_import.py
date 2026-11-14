"""Offline adapter for the pinned official finalpool/<task>/eval_res.json.

Uses the official pass boolean/null field. Missing/invalid files and reserved
evaluation infrastructure failures remain unknown. Importing local files does
not authenticate an external evaluation or prove any model call occurred.
"""
import argparse
import hashlib
import json
from pathlib import Path
import uuid

from workenv.core import contained, no_links
from evaluation.request_contract import validate_request

ROOT = Path(__file__).resolve().parents[1]
INFRASTRUCTURE_FAILURES = {"evaluation_error", "log_file_not_found"}


def read_measurement(path: Path, root: Path):
    try:
        no_links(path, root)
        if not path.is_file(): return {"status": "unknown", "reason": "missing_eval_res"}
        if path.stat().st_size > 1024 * 1024: return {"status": "unknown", "reason": "oversized_eval_res"}
        raw = path.read_bytes()
        value = json.loads(raw)
        checksum = hashlib.sha256(raw).hexdigest()
        if not isinstance(value, dict) or type(value.get("pass")) is not bool:
            return {"status": "unknown", "reason": "missing_or_null_pass", "sha256": checksum}
        if value.get("failure") in INFRASTRUCTURE_FAILURES or value.get("error"):
            return {"status": "unknown", "reason": "evaluator_infrastructure", "sha256": checksum}
        return {"status": "passed" if value["pass"] else "failed", "reason": None, "sha256": checksum}
    except (OSError, ValueError, TypeError):
        return {"status": "unknown", "reason": "unreadable_or_invalid_eval_res"}


def collect(request: dict, result_root: Path):
    request_validation = validate_request(request, ROOT)
    tasks = request["task_ids"]
    result_root = result_root.absolute()
    no_links(result_root, result_root)
    rows = []
    for task_id in tasks:
        path = contained(result_root, "finalpool/" + task_id + "/eval_res.json")
        rows.append({"task_id": task_id, **read_measurement(path, result_root)})
    passed = sum(row["status"] == "passed" for row in rows)
    failed = sum(row["status"] == "failed" for row in rows)
    unknown = len(rows) - passed - failed
    return {"schema_version": "toolathlon-verified-offline-import-v1", "execution_kind": "offline_import_no_execution",
            "authentication": "unverified_external_files", "request_sha256": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
            "checkpoint_descriptor_sha256": request.get("checkpoint_descriptor_sha256"), "rows": rows,
            "request_validation": request_validation, "requested_task_count": len(tasks),
            "valid_result_count": passed + failed, "complete_result_coverage": unknown == 0,
            "observed_valid_coverage": (passed + failed) / len(tasks),
            "observed_passed": passed, "observed_failed": failed, "unknown": unknown,
            "observed_success_rate_valid_only": passed / (passed + failed) if passed + failed else None,
            "official_evaluation_claim": False, "model_calls_performed_by_import": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    args = parser.parse_args()
    report = collect(json.loads(args.request.read_text(encoding="utf-8")), args.results)
    output = ROOT / "evidence/verified" / ("import_" + uuid.uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(output.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__": raise SystemExit(main())
