"""Project-scoped Cordis bundles for the installed DeepSeek Harness desktop."""

import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
import sys

from ...evidence.io import read_json
from ..base import host_record, read_optional
from ..install import _apply, _lock, _settings_path

_APP = Path("/Applications/DeepSeek Harness.app")


def _profile(directory=None):
    root = Path(directory).expanduser() if directory else Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh") / "profiles" / "desktop"
    return root.resolve(strict=True)


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _receipt(project, state_dir):
    return host_record(project, state_dir, "dsh").with_name("desktop.json")


def _managed_package(project, state_dir, registration, node):
    key = hashlib.sha256(str(project).encode()).hexdigest()[:16]
    name = "dsh-plugin-skill-rsi-framework-" + key
    state = host_record(project, state_dir, "dsh").parent
    source = state / "desktop" / name
    anchor = _APP / "Contents/Resources/app.asar/dsh/package.json"
    resource = files("skill_rsi.adapters.dsh")
    modules = {name: resource.joinpath(name).read_bytes() for name in ("plugin.mjs", "baseline.mjs")}
    asset_digest = hashlib.sha256(b"\0".join(modules.values())).hexdigest()
    config = {"project": str(project), "argv": [sys.executable, "-m", "skill_rsi.adapters.bridge", "hook",
              "--host", "dsh", "--project", str(project), "--state-dir", str(state_dir),
              "--owner", registration["registration_id"]],
              "desktop": True, "desktopDigest": asset_digest, "registrationPath": str(state / "registration.json"),
              "brandingAnchor": str(anchor), "observe": registration.get("observe", False), "node": node}
    package = {"name": name, "version": "0.0.0", "private": True, "type": "module", "main": "./index.mjs",
               "exports": {".": "./index.mjs", "./package.json": "./package.json", "./cordis.patch.yml": "./cordis.patch.yml"},
               "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},
               "skillRsi": {"project": str(project), "asset_digest": asset_digest}}
    entry = ("import { apply as applyFramework } from './plugin.mjs';\n"
             f"export const name = {json.dumps(name)};\n"
             "export const inject = ['tools', 'llm'];\n"
             "export function apply(ctx, config) { return applyFramework(ctx, config); }\n")
    modules.update({"index.mjs": entry.encode(), "package.json": _encoded(package),
                    "cordis.patch.yml": _encoded([{"insert": [{"id": name, "name": name, "config": config}]}])})
    return name, source, modules, asset_digest


def install_desktop(project, state_dir, *, profile_dir=None, observe=None, node=None):
    """Install one managed desktop bundle while retaining all other profile entries."""
    project, state_dir = Path(project).expanduser().resolve(strict=True), Path(state_dir).expanduser().resolve()
    registration = read_optional(host_record(project, state_dir, "dsh"))
    if not registration:
        raise ValueError("Configure this DSH project before connecting the desktop application")
    if observe is not None and observe != registration.get("observe", False):
        raise ValueError("Save the project's observation setting before connecting the desktop application")
    if not (_APP / "Contents/Resources/app.asar").is_file():
        raise ValueError(f"DeepSeek Harness desktop runtime is missing: {_APP}")
    profile = _profile(profile_dir)
    manifest_path = _settings_path(profile, "package.json")
    receipt_path = _receipt(project, state_dir)
    executable = registration.get("command") or "dsh"
    command = shutil.which(executable) or executable
    adjacent_node = Path(command).expanduser().parent / "node"
    node = str(Path(node).expanduser().resolve()) if node else shutil.which("node")
    if node is None and adjacent_node.is_file():
        node = str(adjacent_node.resolve())
    name, source, modules, digest = _managed_package(project, state_dir, registration, node)
    target = _settings_path(profile, "node_modules/" + name)
    with _lock(profile / ".skill-rsi-desktop.lock"):
        manifest = read_json(manifest_path)
        dependencies = manifest.setdefault("dependencies", {})
        bundles = manifest.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
        if not isinstance(dependencies, dict) or not isinstance(bundles, list):
            raise ValueError("Desktop profile requires dependencies and dsh.profile.bundles")
        previous = read_optional(receipt_path)
        expected = "file:" + str(source)
        if name in dependencies and dependencies[name] != expected:
            raise ValueError("The desktop bundle name is already managed by another configuration")
        if target.exists():
            known = previous.get("files", {}) if previous else {}
            actual = {str(path.relative_to(target)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in target.rglob("*") if path.is_file() and not path.is_symlink()}
            if not known or actual != known or any(path.is_symlink() for path in target.rglob("*")):
                raise ValueError("The managed desktop bundle was edited. Retain those edits before replacing it")
        dependencies[name] = expected
        if name not in bundles:
            bundles.append(name)
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in modules.items()}
        receipt = {"schema": "skill-rsi.dsh-desktop.v1", "project": str(project), "profile": str(profile),
                   "bundle": name, "source": str(source), "package_path": str(target), "files": hashes,
                   "asset_digest": digest, "registration_id": registration["registration_id"],
                   "node": node,
                   "phase": "registered", "reload_required": True,
                   "notice": "Restart DeepSeek Harness to load this project bundle"}
        updates = [(source / name, raw) for name, raw in modules.items()]
        updates.extend((target / name, raw) for name, raw in modules.items())
        updates.extend([(manifest_path, _encoded(manifest)), (receipt_path, _encoded(receipt))])
        _apply(updates)
    return status_desktop(project, state_dir)


def status_desktop(project, state_dir):
    receipt_path = _receipt(project, state_dir)
    receipt = read_optional(receipt_path)
    if not receipt:
        return {"configured": False, "loaded": False, "reload_required": False}
    loaded = read_optional(receipt_path.with_name("desktop-loaded.json")) or {}
    registration = read_optional(host_record(project, state_dir, "dsh")) or {}
    manifest = read_optional(Path(receipt["profile"]) / "package.json") or {}
    registered = (manifest.get("dependencies", {}).get(receipt["bundle"]) == "file:" + receipt["source"]
                  and receipt["bundle"] in manifest.get("dsh", {}).get("profile", {}).get("bundles", []))
    matches = (loaded.get("asset_digest") == receipt["asset_digest"]
               and loaded.get("registration_id") == registration.get("registration_id")
               and receipt["registration_id"] == registration.get("registration_id") and registered)
    return {**receipt, "configured": registered, "loaded": bool(matches), "reload_required": not matches,
            "runtime": loaded.get("runtime") if matches else None}


def uninstall_desktop(project, state_dir, *, profile_dir=None):
    receipt_path = _receipt(project, state_dir)
    receipt = read_optional(receipt_path)
    if not receipt:
        return {"configured": False, "loaded": False, "reload_required": False}
    profile = _profile(profile_dir or receipt["profile"])
    if str(profile) != receipt["profile"]:
        raise ValueError("Desktop profile differs from the installed bundle")
    target = _settings_path(profile, "node_modules/" + receipt["bundle"])
    with _lock(profile / ".skill-rsi-desktop.lock"):
        manifest_path = _settings_path(profile, "package.json")
        manifest = read_json(manifest_path)
        dependencies = manifest.get("dependencies", {})
        expected = "file:" + receipt["source"]
        if dependencies.get(receipt["bundle"]) not in {None, expected}:
            raise ValueError("The managed desktop dependency was edited")
        dependencies.pop(receipt["bundle"], None)
        bundles = manifest.get("dsh", {}).get("profile", {}).get("bundles", [])
        bundles[:] = [name for name in bundles if name != receipt["bundle"]]
        for name, digest in receipt["files"].items():
            path = _settings_path(target, name)
            if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("The managed desktop bundle was edited")
        _apply([(manifest_path, _encoded(manifest))])
        for name in receipt["files"]:
            (target / name).unlink(missing_ok=True)
        if target.exists() and not any(target.iterdir()):
            target.rmdir()
        receipt_path.unlink()
        receipt_path.with_name("desktop-loaded.json").unlink(missing_ok=True)
    return {"configured": False, "loaded": False, "reload_required": True,
            "notice": "Restart DeepSeek Harness to unload this project bundle"}
