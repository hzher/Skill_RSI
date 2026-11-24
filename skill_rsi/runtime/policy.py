"""Budgeted, scope-limited policy inputs and frozen component execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
import json
import stat

from ..components.manifest import Release, canonical, relative_file, sha256
from .model import RuntimeEvent

DEFAULT_BUDGET = {
    "max_action_bytes": 64 * 1024,
    "max_input_bytes": 4 * 1024 * 1024,
    "max_reads": 1,
    "max_original_bytes": 4 * 1024 * 1024,
    "max_state_bytes": 64 * 1024,
    "max_decision_ms": 250,
}

_ACTION_CAPABILITIES = {
    "replace_tool_result": "replace_tool_result",
    "append_context": "append_context",
    "update_tool_input": "update_tool_input",
    "display_suggestion": "suggest",
    "load_skill": "load_skill",
}


def action_capability(kind: str) -> str:
    return _ACTION_CAPABILITIES.get(kind, kind)


@dataclass(frozen=True, slots=True)
class Original:
    handle: str
    raw: bytes
    digest: str
    source_ref: Any


@dataclass
class PolicyContext:
    """Data visible to a policy at a single, project-bound lifecycle event."""

    project: Path
    project_id: str
    event: RuntimeEvent
    release: Release
    options: dict
    state: dict
    budget: dict
    operation_key: str
    can_read_origin: bool
    reads: int = 0
    originals: list[Original] = field(default_factory=list)

    def artifact_file(self, name: str) -> bytes:
        name = relative_file(name)
        expected = self.release.manifest["files"].get(name)
        if expected is None:
            raise ValueError(f"Undeclared component file: {name}")
        path = self.release.path / name
        if any(item.is_symlink() for item in (path, *path.parents) if item.is_relative_to(self.release.path)):
            raise ValueError(f"Symlink in component file: {path}")
        raw = path.read_bytes()
        if sha256(raw) != expected:
            raise ValueError(f"Component file digest mismatch: {name}")
        return raw

    def read_origin(self, name: str) -> tuple[bytes, dict]:
        """Read one declared, ordinary project file within this event's read budget."""
        if not self.can_read_origin:
            raise PermissionError("Host does not expose a project source reader")
        resource = self.event.payload.get("resource")
        if not isinstance(resource, dict) or resource.get("source_path") != name:
            raise PermissionError("Resource was not declared in the visible event")
        if self.reads >= self.budget["max_reads"]:
            raise ValueError("Component source read budget exceeded")
        self.reads += 1
        name = relative_file(name)
        path = self.project.joinpath(name)
        if any(item.is_symlink() for item in (path, *path.parents) if item.is_relative_to(self.project)):
            raise PermissionError("Symbolic links are outside the project source contract")
        path = path.resolve(strict=True)
        if not path.is_relative_to(self.project) or not stat.S_ISREG(path.stat().st_mode):
            raise PermissionError("Expected an ordinary source file inside the project")
        max_bytes = self.budget["max_original_bytes"]
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("Project source exceeds the original byte budget")
        return raw, {"path": name, "sha256": sha256(raw), "bytes": len(raw), "payload_layer": "project_source"}

    def store_original(self, text: str, source_ref: Any = None) -> str:
        """Stage the exact tool payload. The runtime commits it before any replacement."""
        if text != self.event.payload.get("text"):
            raise ValueError("Only this event's tool payload can be saved as its original")
        raw = text.encode("utf-8")
        if sum(len(original.raw) for original in self.originals) + len(raw) > self.budget["max_original_bytes"]:
            raise ValueError("Component original byte budget exceeded")
        digest = sha256(raw)
        handle = "result-" + sha256(canonical([self.project_id, self.operation_key, digest]))
        self.originals.append(Original(handle, raw, digest, source_ref or self.event.trigger_ref))
        return handle


def propose(context: PolicyContext, adapter: Any) -> Mapping:
    release = context.release
    manifest = release.manifest
    entrypoint = manifest["entrypoint"]
    engine = manifest.get("engine", "skill" if release.form == "skill" else "native")
    if engine == "python" and release.trusted_python:
        namespace = {"__name__": "_skill_rsi_component_" + release.digest, "__file__": str(release.path / entrypoint)}
        exec(compile(context.artifact_file(entrypoint), str(release.path / entrypoint), "exec"), namespace)
        policy = namespace.get("propose")
        if not callable(policy):
            raise ValueError("Python component must define propose(context)")
        result = policy(context)
    elif engine == "skill":
        result = {
            "reason": "skill_trigger_matched",
            "action": {
                "kind": manifest["action"]["kind"],
                "target": manifest["action"].get("target", "context.skill." + release.component_id),
                "payload": {
                    "text": context.artifact_file(entrypoint).decode("utf-8"),
                    "skill_id": release.component_id,
                    "version": release.version,
                    "artifact_digest": release.digest,
                },
            },
        }
    else:
        native = getattr(adapter, "propose_native", None)
        if not callable(native):
            raise ValueError(f"Adapter has no isolated proposal executor for {engine} components")
        result = native(context, engine)
    if result is None:
        return {"reason": "policy_skipped"}
    if not isinstance(result, Mapping):
        raise ValueError("Component proposal must be an object")
    return result


def normalized_action(action: Any, context: PolicyContext) -> dict | None:
    if action is None:
        return None
    manifest = context.release.manifest
    if not isinstance(action, dict) or action.get("kind") != manifest["action"]["kind"]:
        raise ValueError("Component action does not match the frozen action kind")
    target = action.get("target")
    if not isinstance(target, str) or not target or not isinstance(action.get("payload"), dict):
        raise ValueError("Component action requires a target and payload")
    fixed_target = manifest["action"].get("target")
    if fixed_target and target != fixed_target:
        raise ValueError("Component action targets a different object than its manifest")
    result = {
        **action, "boundary": context.event.boundary,
        "expected_input_version": context.event.input_version, "event_id": context.event.event_id,
        "session_id": context.event.session_id,
    }
    if len(canonical(result)) > context.budget["max_action_bytes"]:
        raise ValueError("Component action exceeds its byte budget")
    return result


def state_update(result: Mapping, context: PolicyContext) -> dict | None:
    next_state = result.get("state")
    if next_state is None:
        return None
    if not isinstance(next_state, dict) or not context.release.manifest.get("state_version"):
        raise ValueError("Component state requires an object and a declared state_version")
    raw = canonical(next_state)
    if len(raw) > context.budget["max_state_bytes"]:
        raise ValueError("Component state exceeds its byte budget")
    return json.loads(raw)
