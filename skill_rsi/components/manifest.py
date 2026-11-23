"""Frozen release metadata shared by installers, pipelines and the runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
import hashlib
import json
import re

SCHEMA = "skill-rsi.component.v1"
FORMS = frozenset({"plugin", "mcp", "hook", "gateway", "skill", "middleware"})
MODES = frozenset({"shadow", "suggest", "apply"})
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-]{0,119}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BUDGETS = {
    "max_action_bytes", "max_input_bytes", "max_reads", "max_original_bytes",
    "max_state_bytes", "max_decision_ms", "max_actions_per_session",
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_id(value: Any, field: str = "identifier") -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"Invalid {field}: {value!r}")
    return value


def relative_file(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 500 or "\\" in value or ":" in value:
        raise ValueError(f"Invalid component path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"Expected a relative component path: {value!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"Invalid character in component path: {value!r}")
    return value


def _object(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _strings(value: Any, field: str, *, optional: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{field} must be a list of nonempty strings")
    if not optional and not value:
        raise ValueError(f"{field} must be nonempty")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} contains duplicates")
    return tuple(value)


def required_capabilities(manifest: Mapping, mode: str) -> frozenset[str]:
    requirements = manifest["required_capabilities"]
    if isinstance(requirements, list):
        return frozenset(requirements)
    return frozenset(requirements.get("all", ())) | frozenset(requirements.get(mode, ()))


def artifact_digest(manifest: Mapping) -> str:
    return sha256(canonical({key: value for key, value in manifest.items() if key != "artifact_digest"}))


def validate_manifest(value: Any) -> dict:
    manifest = _object(value, "manifest")
    if manifest.get("schema") != SCHEMA:
        raise ValueError(f"Unsupported component manifest schema: {manifest.get('schema')!r}")
    stable_id(manifest.get("component_id"), "component_id")
    stable_id(manifest.get("version"), "component version")
    form = stable_id(manifest.get("form"), "component form")
    if form not in FORMS:
        raise ValueError(f"Unsupported component form: {form}")
    engine = manifest.get("engine", "skill" if form == "skill" else "native")
    if engine not in {"skill", "native", "python", "wasm"}:
        raise ValueError(f"Unsupported component engine: {engine!r}")
    entrypoint = relative_file(manifest.get("entrypoint"))
    if engine == "python" and not entrypoint.endswith(".py"):
        raise ValueError("A Python component entrypoint must be a .py file")
    if engine == "wasm" and not entrypoint.endswith(".wasm"):
        raise ValueError("A Wasm component entrypoint must be a .wasm file")
    if engine == "skill" and (form != "skill" or not entrypoint.endswith("SKILL.md")):
        raise ValueError("A Skill component entrypoint must be SKILL.md")

    files = _object(manifest.get("files"), "files")
    if entrypoint not in files or any(path.casefold() == "manifest.json" for path in files):
        raise ValueError("files must include the entrypoint and exclude manifest.json")
    for path, digest in files.items():
        relative_file(path)
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError(f"Invalid SHA-256 for {path}")
    if len(files) != len({path.casefold() for path in files}):
        raise ValueError("Component paths collide on a case-insensitive file system")

    requirements = manifest.get("required_capabilities")
    if isinstance(requirements, list):
        _strings(requirements, "required_capabilities", optional=True)
    else:
        for mode, caps in _object(requirements, "required_capabilities").items():
            if mode not in MODES | {"all"}:
                raise ValueError(f"Invalid mode in required_capabilities: {mode}")
            _strings(caps, f"required_capabilities.{mode}", optional=True)

    scope = _object(manifest.get("scope"), "scope")
    _strings(scope.get("hosts"), "scope.hosts")
    if set(_strings(scope.get("modes"), "scope.modes")) - MODES:
        raise ValueError("scope.modes contains an unknown mode")
    for host, versions in _object(scope.get("host_versions", {}), "scope.host_versions").items():
        stable_id(host, "host")
        _strings(versions, f"scope.host_versions.{host}")
    if "adapter_versions" in scope:
        _strings(scope["adapter_versions"], "scope.adapter_versions")

    trigger = _object(manifest.get("trigger"), "trigger")
    _strings(trigger.get("boundaries"), "trigger.boundaries")
    if "tools" in trigger:
        _strings(trigger["tools"], "trigger.tools")
    action = _object(manifest.get("action"), "action")
    stable_id(action.get("kind"), "action.kind")
    if "target" in action:
        _strings([action["target"]], "action.target")
    _strings(action.get("writes"), "action.writes", optional=True)
    if "state_version" in manifest:
        stable_id(manifest["state_version"], "state_version")
    if "builtin_policy" in manifest:
        stable_id(manifest["builtin_policy"], "builtin_policy")
        if engine != "python":
            raise ValueError("builtin_policy requires the Python engine")
    _object(manifest.get("defaults", {}), "defaults")

    budget = _object(manifest.get("budget"), "budget")
    for key, limit in budget.items():
        if key not in _BUDGETS or type(limit) is not int or limit < 0:
            raise ValueError(f"Invalid component budget: {key}={limit!r}")
    if manifest.get("fallback") != "native":
        raise ValueError("A component must declare native fallback")
    if manifest.get("artifact_digest") != artifact_digest(manifest):
        raise ValueError("Component manifest digest mismatch")
    return manifest


@dataclass(frozen=True, slots=True)
class Release:
    manifest: dict
    path: Path
    trusted_python: bool = False

    @property
    def component_id(self) -> str:
        return self.manifest["component_id"]

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def form(self) -> str:
        return self.manifest["form"]

    @property
    def digest(self) -> str:
        return self.manifest["artifact_digest"]

    def to_dict(self) -> dict:
        return {
            "component_id": self.component_id, "version": self.version,
            "form": self.form, "artifact_digest": self.digest, "path": str(self.path),
            "scope": self.manifest["scope"],
        }
