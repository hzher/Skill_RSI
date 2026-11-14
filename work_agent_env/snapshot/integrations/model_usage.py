"""Durable transport observations, without inferring model execution from HTTP.

Metadata only: prompts, responses, API keys and cookies are never persisted here.
"""
import hashlib
import json
import os
from pathlib import Path
import time
import uuid


class ModelUsageLedger:
    def __init__(self, directory: Path):
        self.directory = directory
        if directory.is_symlink(): raise ValueError("Model evidence directory cannot be a symbolic link")
        directory.mkdir(parents=True, exist_ok=True)

    def _write(self, path, value):
        raw = json.dumps(value, sort_keys=True, allow_nan=False).encode()
        if len(raw) > 16384: raise ValueError("Model transport metadata exceeds budget")
        temporary = self.directory / (uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists(): temporary.unlink()

    def begin(self, *, actor, model_identity, episode_id=None, training_run_id=None):
        if actor not in ("generator", "executor") or not isinstance(model_identity, str) or not 0 < len(model_identity) <= 500:
            raise ValueError("Invalid model transport identity")
        event_id = uuid.uuid4().hex
        value = {"schema_version": "model-transport-observation-v1", "event_id": event_id, "actor": actor,
                 "model_identity": model_identity, "episode_id": episode_id, "training_run_id": training_run_id,
                 "status": "started", "started_unix": time.time(), "inference_attestation": None,
                 "scope": "transport_observation_only_not_training_or_model_attestation"}
        self._write(self.directory / (event_id + ".json"), value)
        return ModelObservation(self, value)

    def summarize(self):
        counts = {actor: {state: 0 for state in ("started", "response_received", "failed")} for actor in ("generator", "executor")}
        for index, path in enumerate(self.directory.glob("*.json")):
            if index >= 10000 or path.is_symlink() or path.stat().st_size > 16384:
                raise ValueError("Model transport ledger exceeds supported inventory")
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema_version") != "model-transport-observation-v1": raise ValueError("Unknown transport evidence schema")
            counts[value["actor"]][value["status"]] += 1
        return {"counts_by_actor": counts, "attested_model_inferences": None,
                "training_updates": None, "scope": "transport_observation_only"}


class ModelObservation:
    def __init__(self, ledger, value):
        self.ledger, self.value = ledger, value

    def _finish(self, **values):
        if self.value["status"] != "started": raise ValueError("Model observation is already terminal")
        update = {**self.value, **values, "ended_unix": time.time()}
        self.ledger._write(self.ledger.directory / (self.value["event_id"] + ".json"), update)
        self.value = update

    def response(self, response_id, response):
        if not isinstance(response_id, str) or not 0 < len(response_id) <= 500: raise ValueError("Missing actual provider response identity")
        raw = json.dumps(response, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
        self._finish(status="response_received", response_id=response_id, response_sha256=hashlib.sha256(raw).hexdigest())

    def failed(self, error_type):
        if not isinstance(error_type, str) or not 0 < len(error_type) <= 100: raise ValueError("Use a bounded error type without request data")
        self._finish(status="failed", error_type=error_type)
