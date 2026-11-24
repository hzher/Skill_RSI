"""Project-local native registration with scoped ownership."""

import fcntl
import hashlib
import json
import os
import shlex
import sys
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from ..evidence.io import inside
from ..runtime import project_identity
from .base import host_record, read_optional

_HOOKS = {
    "claude_code": ("SessionStart", "PreCompact", "PostToolUse", "PostToolUseFailure", "PreToolUse", "Stop"),
    "codex": ("SessionStart", "UserPromptSubmit", "PostToolUse", "PreToolUse", "Stop"),
}


def _settings_path(project, path):
    target = project
    for part in Path(path).parts:
        target = target / part
        if target.is_symlink():
            raise ValueError(f"Refusing to manage a symlinked setting: {target}")
    return target


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_text(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        try:
            temporary.write(contents)
            temporary.flush()
            os.replace(temporary.name, path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)


@dataclass(frozen=True)
class _Bridge:
    host: str
    project: Path
    state_dir: Path
    owner: str
    python: str

    def argv(self, mode):
        return [self.python, "-m", "skill_rsi.adapters.bridge", mode, "--host", self.host,
                "--project", str(self.project), "--state-dir", str(self.state_dir), "--owner", self.owner]

    def hook(self):
        return {"hooks": [{"type": "command", "command": shlex.join(self.argv("hook")), "timeout": 15}]}

    def server(self):
        command, *args = self.argv("mcp")
        return {"command": command, "args": args}

    def toml(self):
        server = self.server()
        return (f"\n# Skill RSI {self.owner} begin\n[mcp_servers.skill-rsi]\n"
                f"command = {json.dumps(self.python, ensure_ascii=False)}\n"
                f"args = {json.dumps(server['args'], ensure_ascii=False)}\n# Skill RSI {self.owner} end\n")


def _hooks(bridge, prior, *, remove, corroborate):
    config = ".claude/settings.local.json" if bridge.host == "claude_code" else ".codex/hooks.json"
    path = _settings_path(bridge.project, config)
    data = read_optional(path)
    if data is None and remove and not corroborate:
        return path, None
    data = data or {}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"Expected a hooks mapping in {path}")
    old, new = prior.hook(), bridge.hook()
    matched = False
    for boundary in _HOOKS[bridge.host]:
        values = hooks.get(boundary, [])
        if not isinstance(values, list):
            raise ValueError(f"Expected a hook list in {path}: {boundary}")
        updated = []
        for value in values:
            if value == old:
                matched = True
            elif value != new:
                raw = json.dumps(value)
                if prior.owner in raw or bridge.owner in raw:
                    raise ValueError(f"Managed {boundary} hook was edited: {path}")
                updated.append(value)
                continue
            if not remove and new not in updated:
                updated.append(new)
        if not remove and new not in updated:
            updated.append(new)
        if updated:
            hooks[boundary] = updated
        else:
            hooks.pop(boundary, None)
    if corroborate and not matched:
        raise ValueError(f"Cannot verify the previous MCP interpreter against a managed hook in {path}")
    if not hooks:
        data.pop("hooks")
    return path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()


def _settings(bridge, receipt, *, remove=False):
    claude = bridge.host == "claude_code"
    path = _settings_path(bridge.project, ".mcp.json" if claude else ".codex/config.toml")
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    data = (json.loads(before) if claude else tomllib.loads(before)) if before else {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a configuration object in {path}")
    table = "mcpServers" if claude else "mcp_servers"
    servers = data.get(table, {})
    if not isinstance(servers, dict):
        raise ValueError(f"Expected an MCP server mapping in {path}")
    key = "skill-rsi"
    server = servers.get(key)
    python = receipt.get("bridge_python")
    # Older receipts did not record an interpreter. Cross-check the canonical MCP
    # argv against an independently stored hook before migrating an unknown path.
    if receipt and python is None and isinstance(server, dict):
        python = server.get("command")
    if python is not None and (not isinstance(python, str) or not os.path.isabs(python)):
        raise ValueError(f"Invalid managed MCP interpreter in {path}")
    prior = replace(bridge, owner=receipt.get("registration_id", bridge.owner), python=python or bridge.python)
    if key in servers and server != prior.server():
        raise ValueError(f"MCP entry belongs to another configuration: {path}")
    if claude:
        if remove:
            servers.pop(key, None)
        else:
            servers[key] = bridge.server()
        if servers:
            data[table] = servers
        else:
            data.pop(table, None)
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    elif key in servers:
        block = prior.toml()
        if before.count(block) != 1:
            raise ValueError(f"Managed MCP block was edited: {path}")
        content = before.replace(block, "" if remove else bridge.toml(), 1)
    else:
        if f"# Skill RSI {prior.owner} " in before:
            raise ValueError(f"Managed MCP block was edited: {path}")
        content = before if remove else before + bridge.toml()
    hook = _hooks(bridge, prior, remove=remove, corroborate=(
        bool(receipt) and "bridge_python" not in receipt and prior.python != bridge.python
    ))
    return [hook, (path, None if remove and not path.exists() else content.encode())]


def _dsh_patch(bridge, runtime, profile, command):
    directory = runtime.parent
    modules = ("plugin.mjs", "baseline.mjs")
    resources = files("skill_rsi.adapters.dsh")
    payload = [resources.joinpath(name).read_bytes() for name in modules]
    digest = hashlib.sha256(b"\0".join(payload)).hexdigest()
    plugin_dir = directory / ("plugin-" + digest[:16])
    for module, contents in zip(modules, payload, strict=True):
        _write_text(plugin_dir / module, contents)
    resource = plugin_dir / "plugin.mjs"
    config = {"argv": bridge.argv("hook"), "project": str(bridge.project), "registrationId": bridge.owner}
    patch = directory / "cordis.patch.json"
    content = json.dumps([{"insert": [{"id": "skill-rsi", "name": resource.as_uri(), "config": config}]}],
                         ensure_ascii=False, indent=2).encode()
    return {"patch": str(patch), "patch_sha256": hashlib.sha256(content).hexdigest(),
            "profile": profile, "resource_path": str(plugin_dir), "resource_digest": digest,
            "command_prefix": [command, "--profile", profile, "--patch", str(patch)]}, (patch, content)


def _check_patch(record, receipt):
    patch = record.parent / "cordis.patch.json"
    if patch.is_symlink() or (patch.exists() and (receipt.get("patch") != str(patch) or
            hashlib.sha256(patch.read_bytes()).hexdigest() != receipt.get("patch_sha256"))):
        raise ValueError(f"Managed DSH patch was edited: {patch}")
    return patch


def _apply(updates):
    previous = {path: path.read_bytes() if path.exists() else None for path, content in updates if content is not None}
    done = []
    try:
        for path, content in updates:
            if content is not None and previous[path] != content:
                if (path.read_bytes() if path.exists() else None) != previous[path]:
                    raise ValueError(f"Host settings changed during registration: {path}")
                _write_text(path, content)
                done.append(path)
    except (OSError, ValueError):
        for path in reversed(done):
            if previous[path] is None:
                path.unlink(missing_ok=True)
            else:
                _write_text(path, previous[path])
        raise


def _scope_options(project, previous, options):
    values = {}
    for key in ("mcp_paths", "checkpoint_paths"):
        entries = options.get(key, previous.get(key, []))
        if not isinstance(entries, list) or not all(isinstance(item, str) for item in entries):
            raise ValueError(f"{key} requires a list of project-relative paths")
        for item in entries:
            inside(project, item)
        values[key] = entries
    constraints = options.get("constraints", previous.get("constraints", []))
    if not isinstance(constraints, list):
        raise ValueError("constraints requires a list of approved source descriptors")
    for resource in constraints:
        if not isinstance(resource, dict) or not all(isinstance(resource.get(key), str) and resource[key] for key in (
            "source_path", "object_id", "field",
        )):
            raise ValueError("A constraint requires source_path, object_id and field")
        inside(project, resource["source_path"])
        targets = resource.get("target_paths", [])
        if not isinstance(targets, list) or not all(isinstance(target, str) for target in targets):
            raise ValueError("Constraint target_paths requires a list of project-relative paths")
        for target in targets:
            inside(project, target)
        if resource.get("pointer") is not None and (not isinstance(resource["pointer"], str) or
            (resource["pointer"] and not resource["pointer"].startswith("/"))):
            raise ValueError("A constraint pointer must be a JSON Pointer")
    values["constraints"] = constraints
    return values


def install(adapter, project, state_dir, *, observe=None, profile=None, command=None, **options):
    project, state_dir = Path(project).resolve(strict=True), Path(state_dir).resolve()
    if not project.is_dir():
        raise ValueError(f"Expected a project directory: {project}")
    target = host_record(project, state_dir, adapter.name)
    lock = _settings_path(project, ".skill-rsi-install.lock")
    with _lock(lock):
        existing = read_optional(target) or {}
        if command is None:
            command = existing.get("command", adapter.command)
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ValueError("command must be a native CLI executable name or path")
        if os.sep in command:
            command = os.path.abspath(project / Path(command).expanduser())
        profile = profile or existing.get("profile") or ("sdk" if adapter.name == "dsh" else None)
        identity = {"adapter_version": adapter.version, "bridge_python": sys.executable, "command": command,
                    "native_host_version": adapter.probe(project, command=command).get("host_version"),
                    "profile": profile}
        owner = existing.get("registration_id")
        if not owner or any(existing.get(key) != value for key, value in identity.items()):
            owner = uuid4().hex
        bridge = _Bridge(adapter.name, project, state_dir, owner, sys.executable)
        entry = {**identity, "registration_id": owner, "project": str(project), "project_id": project_identity(project),
                 "observe": bool(existing.get("observe")) if observe is None else bool(observe),
                 "host": adapter.name, "phase": "registered",
                 "journal": str(target.parent / "journal"),
                 **_scope_options(project, existing, options)}
        updates = []
        if adapter.name == "dsh":
            _check_patch(target, existing)
            details, patch = _dsh_patch(bridge, target, profile, command)
            entry.update(details)
            updates.append(patch)
        else:
            updates = _settings(bridge, existing)
            entry["managed_settings"] = [str(path) for path, _ in updates]
        updates.append((target, (json.dumps(entry, ensure_ascii=False, indent=2) + "\n").encode()))
        _apply(updates)
        return entry


def uninstall(adapter, project, state_dir, **options):
    project, state_dir = Path(project).resolve(strict=True), Path(state_dir).resolve()
    target = host_record(project, state_dir, adapter.name)
    with _lock(_settings_path(project, ".skill-rsi-install.lock")):
        entry = read_optional(target)
        if not entry:
            return {"host": adapter.name, "phase": "absent"}
        owner = entry["registration_id"]
        if adapter.name == "dsh":
            _check_patch(target, entry).unlink(missing_ok=True)
        else:
            bridge = _Bridge(adapter.name, project, state_dir, owner, sys.executable)
            _apply(_settings(bridge, entry, remove=True))
        target.unlink()
        return {"host": adapter.name, "phase": "unregistered", "registration_id": owner}
