"""Project-local native registration with scoped ownership."""

import fcntl
import hashlib
import json
import os
import shlex
import sys
import tomllib
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from ..evidence.archive import write
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


def _command(host, project, state_dir, mode):
    return [sys.executable, "-m", "skill_rsi.adapters.bridge", mode, "--host", host,
            "--project", str(project), "--state-dir", str(state_dir)]


def _hooks(project, state_dir, host, owner, *, remove=False):
    config = (".claude/settings.local.json" if host == "claude_code" else ".codex/hooks.json")
    path = _settings_path(project, config)
    if remove and not path.exists():
        return path, None
    data = read_optional(path) or {}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"Expected a hooks mapping in {path}")
    for boundary in _HOOKS[host]:
        command = shlex.join(_command(host, project, state_dir, "hook") + ["--owner", owner])
        registration = {"hooks": [{"type": "command", "command": command, "timeout": 15}]}
        values = hooks.get(boundary, [])
        if not isinstance(values, list):
            raise ValueError(f"Expected a hook list in {path}: {boundary}")
        if remove:
            if any(value != registration and owner in json.dumps(value) for value in values):
                raise ValueError(f"Managed {boundary} hook was edited: {path}")
            kept = [value for value in values if value != registration]
            if kept:
                hooks[boundary] = kept
            else:
                hooks.pop(boundary, None)
        elif registration not in values:
            amended = [value for value in values if owner in json.dumps(value)]
            if amended:
                raise ValueError(f"Managed {boundary} hook was edited: {path}")
            hooks[boundary] = [*values, registration]
    if not hooks:
        data.pop("hooks")
    return path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()


def _mcp(project, state_dir, host, owner, *, remove=False):
    args = _command(host, project, state_dir, "mcp") + ["--owner", owner]
    command = {"command": args[0], "args": args[1:]}
    key = "skill-rsi"
    if host == "claude_code":
        path = _settings_path(project, ".mcp.json")
        if remove and not path.exists():
            return path, None
        data = read_optional(path) or {}
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError(f"Expected an MCP server mapping in {path}")
        previous = servers.get(key)
        if previous is not None and previous != command:
            raise ValueError(f"MCP entry belongs to another configuration: {path}")
        if remove:
            servers.pop(key, None)
        else:
            servers[key] = command
        return path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    path = _settings_path(project, ".codex/config.toml")
    if remove and not path.is_file():
        return path, None
    before = path.read_text(encoding="utf-8") if path.is_file() else ""
    config = tomllib.loads(before)
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"Expected an MCP server table in {path}")
    block = (f"\n# Skill RSI {owner} begin\n[mcp_servers.{key}]\n"
             f"command = {json.dumps(args[0], ensure_ascii=False)}\n"
             f"args = {json.dumps(args[1:], ensure_ascii=False)}\n# Skill RSI {owner} end\n")
    if key in servers and block not in before:
        raise ValueError(f"MCP entry belongs to another configuration: {path}")
    final = before.replace(block, "", 1) if remove else before if block in before else before + ("\n" if before and not before.endswith("\n") else "") + block
    return path, final.encode()


def _dsh_patch(project, state_dir, owner, runtime, profile):
    directory = runtime.parent
    modules = ("plugin.mjs", "baseline.mjs")
    resources = files("skill_rsi.adapters.dsh")
    payload = [resources.joinpath(name).read_bytes() for name in modules]
    digest = hashlib.sha256(b"\0".join(payload)).hexdigest()
    plugin_dir = directory / ("plugin-" + digest[:16])
    for module, contents in zip(modules, payload, strict=True):
        _write_text(plugin_dir / module, contents)
    resource = plugin_dir / "plugin.mjs"
    config = {"argv": _command("dsh", project, state_dir, "hook") + ["--owner", owner],
              "project": str(project), "registrationId": owner}
    patch = directory / "cordis.patch.json"
    write(patch, [{"insert": [{"id": "skill-rsi", "name": resource.as_uri(), "config": config}]}])
    return {"patch": str(patch), "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
            "profile": profile, "resource_path": str(plugin_dir), "resource_digest": digest,
            "command_prefix": ["dsh", "--profile", profile, "--patch", str(patch)]}


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
        owner = existing.get("registration_id") or uuid4().hex
        profile = profile or existing.get("profile") or ("sdk" if adapter.name == "dsh" else None)
        entry = {"registration_id": owner, "project": str(project), "project_id": project_identity(project),
                 "observe": bool(existing.get("observe")) if observe is None else bool(observe),
                 "adapter_version": adapter.version, "host": adapter.name, "profile": profile, "phase": "registered",
                 "native_host_version": adapter.probe(project, command=command).get("host_version"),
                 "journal": str(target.parent / "journal"),
                 **_scope_options(project, existing, options)}
        updates = []
        if adapter.name == "dsh":
            entry.update(_dsh_patch(project, state_dir, owner, target, profile))
        else:
            updates = [_hooks(project, state_dir, adapter.name, owner), _mcp(project, state_dir, adapter.name, owner)]
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
            if entry.get("patch"):
                Path(entry["patch"]).unlink(missing_ok=True)
        else:
            _apply([_hooks(project, state_dir, adapter.name, owner, remove=True),
                    _mcp(project, state_dir, adapter.name, owner, remove=True)])
        target.unlink()
        return {"host": adapter.name, "phase": "unregistered", "registration_id": owner}
