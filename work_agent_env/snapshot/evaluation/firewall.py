"""Fail-closed metadata firewall; Verified task bodies are not training inputs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


class ContaminationError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class EvaluationFirewall:
    def __init__(self, manifest: dict, registry: dict):
        if manifest.get("schema_version") != "toolathlon-verified-metadata-v1" or manifest.get("source") != "toolathlon_verified":
            raise ContaminationError("Invalid independent evaluation manifest")
        if not re.fullmatch(r"[0-9a-f]{40}", manifest.get("commit", "")):
            raise ContaminationError("Evaluation source must be pinned")
        self.tasks = set(manifest.get("task_ids", []))
        self.families = set(manifest.get("family_roots", []))
        if not self.tasks or len(self.tasks) != len(manifest["task_ids"]) or not self.families:
            raise ContaminationError("Independent evaluation identity inventory is incomplete")
        if registry.get("schema_version") != "toolathlon-training-seeds-v1":
            raise ContaminationError("Invalid training seed registry")
        if registry.get("evaluation_manifest_sha256") != digest(manifest):
            raise ContaminationError("Training registry was reviewed against a different evaluation inventory")
        self.allowed = {}
        for row in registry.get("admitted_seeds", []):
            key = row["seed_sha256"]
            if key in self.allowed:
                raise ContaminationError("Duplicate training seed identity")
            self.allowed[key] = row

    @classmethod
    def load(cls, manifest_path: Path, registry_path: Path):
        return cls(json.loads(manifest_path.read_text(encoding="utf-8")),
                   json.loads(registry_path.read_text(encoding="utf-8")))

    def check_training_record(self, provenance):
        permitted = {"source", "split", "task_id", "task_family", "source_commit", "seed_sha256", "prompt_sha256", "ancestor_tasks", "ancestor_families"}
        if not isinstance(provenance, dict) or set(provenance) - permitted:
            raise ContaminationError("Training provenance contains unexpected fields or answer material")
        if provenance.get("source") != "toolathlon_gym" or provenance.get("split") != "train":
            raise ContaminationError("Verified, diagnostic and development records cannot enter Generator training")
        for field, length in (("source_commit", 40), ("seed_sha256", 64), ("prompt_sha256", 64)):
            if not re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", provenance.get(field, "")):
                raise ContaminationError("Missing or invalid reviewed seed identity: " + field)
        tasks = {provenance.get("task_id"), *provenance.get("ancestor_tasks", [])}
        families = {provenance.get("task_family"), *provenance.get("ancestor_families", [])}
        if tasks & self.tasks or families & self.families:
            raise ContaminationError("Training task or a parent group overlaps independent evaluation")
        approved = self.allowed.get(provenance.get("seed_sha256"))
        if approved is None or approved != provenance:
            raise ContaminationError("Seed lineage has not been admitted by independent metadata review")
        return True

    def check_evaluation(self, task_ids, *, model_checkpoint, training_registry_sha256, expected_registry_sha256):
        if not task_ids or len(task_ids) != len(set(task_ids)) or not set(task_ids) <= self.tasks:
            raise ContaminationError("Evaluation tasks must be a unique subset of the pinned Verified inventory")
        if not model_checkpoint or training_registry_sha256 != expected_registry_sha256:
            raise ContaminationError("Evaluation must bind a frozen checkpoint and its actual training registry")
        return True
