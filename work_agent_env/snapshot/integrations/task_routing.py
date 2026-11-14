"""Qualify an original task before resource allocation; never run an agent."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from integrations.task_profiles import PROFILE_PATH, build_registry, read_owned, resolve_profile
from workenv.core import ResourceError


class TaskRouter:
    def __init__(self, root, backend):
        if backend not in ("shared", "isolated"):
            raise ValueError("Unknown original-task resource backend")
        self.root, self.backend = Path(root).resolve(), backend
        try:
            self.registry = json.loads(read_owned(self.root, PROFILE_PATH))
            if self.registry != build_registry(self.root):
                raise ValueError("Adapter profile is stale")
        except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
            raise ResourceError("Current original-task profile catalog is unavailable") from exc
        self.catalog_sha256 = hashlib.sha256(read_owned(self.root, PROFILE_PATH)).hexdigest()

    def authorize(self, body):
        if (not isinstance(body, dict) or set(body) != {"resources_session_id", "episode_id", "task_id", "task_data"}
            or type(body["task_data"]) is not dict or body["task_data"]):
            raise ValueError("Original-task seed requires an empty task_data object and exact fields")
        task = body["task_id"]
        if (not isinstance(task, dict) or set(task) != {"taskset", "task_id"}
            or not isinstance(task["taskset"], str)
            or not re.fullmatch(r"toolathlon_gym:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", task["taskset"])):
            raise ValueError("Only the original Toolathlon-GYM source namespace is accepted")
        matches = [p for p in self.registry["profiles"].values() if p["task_id"] == task["task_id"]]
        if len(matches) != 1:
            raise ValueError("Task has no unique source-bound resource adapter")
        profile = matches[0]
        try:
            if hashlib.sha256(read_owned(self.root, PROFILE_PATH)).hexdigest() != self.catalog_sha256:
                raise ValueError("Profile catalog changed after resource startup")
            resolved = resolve_profile(self.registry, task_id=task["task_id"], source_commit=profile["source_commit"],
                                       task_config_sha256=profile["task_config_sha256"], resource_backend=self.backend, root=self.root)
        except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
            raise ResourceError("Original-task routing identity changed; reload idle resources") from exc
        route = {"schema_version": "original-task-route-v1", "profile_id": profile["profile_id"],
                "profile_sha256": resolved["profile_sha256"], "catalog_sha256": self.catalog_sha256,
                "source": profile["source"], "source_commit": profile["source_commit"],
                "task_id": profile["task_id"], "task_config_sha256": profile["task_config_sha256"],
                "resource_backend": self.backend, "mcp_methods": deepcopy(profile["mcp_methods"]),
                "artifact_tools": list(profile["artifact_tools"]), "training_admission": False}
        if "measurement_mode" in profile:
            route.update(measurement_mode=profile["measurement_mode"], reward_version=profile["reward_version"],
                         official_benchmark_score_available=False)
        if "original_service_aliases" in profile:
            route['original_service_aliases'] = deepcopy(profile['original_service_aliases'])
        return route
