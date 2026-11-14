"""Dispatch to deployment-registered evaluators; never import code named by task data."""
import asyncio
from copy import deepcopy
import inspect
import hashlib
import json
import math


def normalize_score(raw):
    if not isinstance(raw, dict):
        raise ValueError("Evaluator must return a JSON object")
    json.dumps(raw, allow_nan=False)  # The complete raw receipt must cross JSON intact.
    # Preserve benchmark-defined continuous reward when supplied.
    masked = raw.get("mask_sample", False)
    if type(masked) is not bool:
        raise ValueError("Invalid sample mask")
    passed = raw.get("pass", raw.get("passed"))
    if passed is not None and type(passed) is not bool:
        raise ValueError("Invalid pass flag")
    failure = raw.get("failure")
    infrastructure = isinstance(failure, str) and failure in ("evaluation_error", "log_file_not_found")
    reward = raw.get("reward", float(passed) if type(passed) is bool else None)
    if reward is not None and (type(reward) not in (int, float) or not math.isfinite(reward)):
        raise ValueError("Invalid evaluator reward")
    valid = not masked and not infrastructure and reward is not None
    return {"valid": valid, "reward": float(reward) if valid else None,
        "passed": passed if valid else None, "mask_sample": not valid,
        "error": None if valid else "evaluation_unavailable", "raw": deepcopy(raw)}


class VerifierRegistry:
    def __init__(self):
        self._handlers = {}

    def register(self, name, handler, *, version):
        if not isinstance(name, str) or not name or name in self._handlers or not callable(handler):
            raise ValueError("Duplicate or invalid verifier")
        if not isinstance(version, str) or not version:
            raise ValueError("Verifier version is required")
        self._handlers[name] = (handler, version)

    def resolve(self, name):
        if name not in self._handlers:
            raise ValueError("Unsupported verifier backend: " + name)
        return self._handlers[name]


class VerifierService:
    def __init__(self, registry, *, timeout=180):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Invalid verifier timeout")
        self.registry, self.timeout = registry, timeout

    async def evaluate(self, task, context):
        # Absolute imports are intentionally local: native containers may mount
        # this package as /opt/workenv_verifiers without the project installed.
        handler, version = self.registry.resolve(task["evaluation"]["backend"])
        identity = {"schema_version": "workagent-score-v1", "task_uid": task["uid"],
            "verifier_input_sha256": hashlib.sha256(json.dumps(task, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
            "task_source_revision": task["source"]["revision"],
            "backend": task["evaluation"]["backend"], "verifier_version": version,
            "training_admitted": False}
        async def invoke():
            if inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(getattr(handler, "__call__", None)):
                return await handler(deepcopy(task), deepcopy(context))
            value = await asyncio.to_thread(handler, deepcopy(task), deepcopy(context))
            return await value if inspect.isawaitable(value) else value
        try:
            raw = await asyncio.wait_for(invoke(), self.timeout)
            score = normalize_score(raw)
        except Exception as exc:
            score = {"valid": False, "reward": None, "passed": None, "mask_sample": True,
                "error": type(exc).__name__, "raw": None}
        return {**identity, **score}
