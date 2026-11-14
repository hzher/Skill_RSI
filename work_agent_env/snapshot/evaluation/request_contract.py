"""Validate offline request metadata; this does not authenticate model execution."""
from datetime import datetime
import hashlib
import json
import re

from evaluation.task_inventory import REQUEST_SCHEMA, load_index, requested_tasks
from workenv.core import no_links

FIELDS = {"schema_version", "created_at", "purpose", "status", "upstream_commit", "task_ids",
          "checkpoint_descriptor_sha256", "training_registry_sha256", "evaluation_manifest_sha256",
          "evaluation_task_index_sha256", "real_model_calls", "scores", "solutions_loaded"}
DIGEST_FIELDS = ("checkpoint_descriptor_sha256", "training_registry_sha256", "evaluation_manifest_sha256", "evaluation_task_index_sha256")


def validate_request(request, root):
    if not isinstance(request, dict) or request.get("schema_version") != REQUEST_SCHEMA:
        raise ValueError("A v2 independent evaluation request bound to typed tasks is required")
    index, index_sha = load_index(root)
    requested_tasks(request.get("task_ids"), index)
    if set(request) != FIELDS:
        raise ValueError("Evaluation request has missing or unexpected metadata fields")
    if (request["purpose"] != "independent_evaluation_only" or request["status"] != "prepared_not_executed"
        or request["scores"] is not None or request["solutions_loaded"] is not False
        or type(request["real_model_calls"]) is not int or request["real_model_calls"] != 0):
        raise ValueError("Offline request must remain unexecuted, scoreless and solution-free")
    if any(not isinstance(request[name], str) or not re.fullmatch("[0-9a-f]{64}", request[name]) for name in DIGEST_FIELDS):
        raise ValueError("Frozen checkpoint and inventory identities must be SHA256 digests")
    try:
        created = datetime.fromisoformat(request["created_at"])
        if created.utcoffset() is None: raise ValueError("Missing timezone")
    except (ValueError, TypeError): raise ValueError("Evaluation request creation time must include a timezone") from None
    manifest_path, registry_path = root / "evaluation/toolathlon_verified.manifest.json", root / "training/seed_registry.json"
    for path in (manifest_path, registry_path):
        no_links(path, root)
        if not path.is_file() or path.stat().st_size > 1024 * 1024: raise ValueError("Missing or oversized evaluation lineage metadata")
    manifest_raw, registry_raw = manifest_path.read_bytes(), registry_path.read_bytes()
    manifest = json.loads(manifest_raw)
    if (request["upstream_commit"] != manifest["commit"] or request["evaluation_task_index_sha256"] != index_sha
        or request["evaluation_manifest_sha256"] != hashlib.sha256(manifest_raw).hexdigest()
        or request["training_registry_sha256"] != hashlib.sha256(registry_raw).hexdigest()):
        raise ValueError("Evaluation request inventory or training registry bytes changed; prepare a new request")
    return {"validation_scope": "schema_and_current_metadata_bytes_not_authentication",
            "checkpoint_descriptor_sha256": request["checkpoint_descriptor_sha256"],
            "training_registry_sha256": request["training_registry_sha256"], "requested_task_count": len(request["task_ids"]),
            "model_execution_verified": False}
