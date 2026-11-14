"""Prepare an isolated evaluation request for an already frozen local checkpoint.

This creates a reviewable offline manifest. It does not contact the public service,
read benchmark answers, run a model, or label an unexecuted evaluation successful.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from evaluation.firewall import EvaluationFirewall
from evaluation.task_inventory import load_index, requested_tasks, REQUEST_SCHEMA
from evaluation.request_contract import validate_request
from training.checkpoint_manifest import verify as verify_checkpoint

ROOT = Path(__file__).resolve().parents[1]


def prepare(checkpoint: Path, task_ids: list[str]):
    manifest_path = ROOT / "evaluation/toolathlon_verified.manifest.json"
    registry_path = ROOT / "training/seed_registry.json"
    guard = EvaluationFirewall.load(manifest_path, registry_path)
    task_index, task_index_sha = load_index(ROOT)
    requested_tasks(task_ids, task_index)
    if checkpoint.is_symlink():
        raise ValueError("Checkpoint descriptor cannot be a symbolic link")
    checkpoint = checkpoint.resolve(strict=True)
    if not checkpoint.is_file() or checkpoint.stat().st_size > 1024 * 1024:
        raise ValueError("Provide a frozen checkpoint JSON descriptor up to 1 MiB")
    descriptor = json.loads(checkpoint.read_text(encoding="utf-8"))
    if descriptor.get("schema_version") != "workagent-frozen-checkpoint-v1" or not descriptor.get("model_sha256") or not descriptor.get("fixed_executor_id"):
        raise ValueError("Descriptor must identify a frozen Generator checkpoint and Executor")
    # Stream potentially large descriptors rather than loading model weights into RAM.
    with checkpoint.open("rb") as source:
        checkpoint_sha = hashlib.file_digest(source, "sha256").hexdigest()
    registry_sha = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    verify_checkpoint(descriptor, registry_sha)
    guard.check_evaluation(task_ids, model_checkpoint=checkpoint_sha,
                           training_registry_sha256=descriptor.get("training_registry_sha256"), expected_registry_sha256=registry_sha)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = {"schema_version": REQUEST_SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
              "purpose": "independent_evaluation_only", "status": "prepared_not_executed",
              "upstream_commit": manifest["commit"], "task_ids": task_ids,
              "checkpoint_descriptor_sha256": checkpoint_sha, "training_registry_sha256": registry_sha,
              "evaluation_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              "evaluation_task_index_sha256": task_index_sha,
              "real_model_calls": 0, "scores": None, "solutions_loaded": False}
    validate_request(result, ROOT)
    output = ROOT / "evidence/verified" / ("request_" + uuid.uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    (output / "request.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-descriptor", type=Path, required=True)
    parser.add_argument("--task-id", action="append", required=True)
    args = parser.parse_args()
    print(prepare(args.checkpoint_descriptor, args.task_id).relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__": raise SystemExit(main())
