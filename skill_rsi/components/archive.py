"""Build and read portable harness extensions from wheel resources, directories or ZIPs."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files as resource_files
import json
import os
from pathlib import Path
import stat
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from .manifest import Release, artifact_digest, canonical, relative_file, sha256, stable_id, validate_manifest

_MANIFEST = "manifest.json"
_MAX_BYTES = 128 * 1024 * 1024
_MAX_FILES = 2048
_CONFIGURABLE_POLICIES = frozenset({"tool-result-view", "constraint-check"})


def _resources():
    return resource_files("skill_rsi.components.resources")


def _builtin(component_id: str):
    root = _resources().joinpath(stable_id(component_id, "component_id"))
    if not root.joinpath(_MANIFEST).is_file():
        raise ValueError(f"Unknown built-in component: {component_id}")
    manifest, entries = read_directory(root, allow_template=True)
    if manifest["component_id"] != component_id:
        raise ValueError(f"Built-in component directory and manifest identifiers differ: {component_id}")
    return manifest, entries


def builtin_catalog() -> tuple[dict, ...]:
    """List releases shipped with this distribution, independently of local installations."""
    releases = []
    directories = sorted(_resources().iterdir(), key=lambda item: item.name)
    for item in directories:
        if not item.joinpath(_MANIFEST).is_file():
            continue
        manifest, _ = _builtin(item.name)
        releases.append({key: manifest[key] for key in ("component_id", "version", "form", "scope", "artifact_digest")})
    return tuple(releases)


def _walk(source, prefix: str = "", *, declared_files=None):
    for path in sorted(source.iterdir(), key=lambda item: item.name):
        name = relative_file(prefix + path.name)
        generated = path.name in {"__pycache__", ".DS_Store"} or path.name.endswith((".pyc", ".pyo"))
        if generated and declared_files is not None:
            if not any(item == name or item.startswith(name + "/") for item in declared_files):
                continue
        if isinstance(path, Path) and path.is_symlink():
            raise ValueError(f"Symbolic link in component: {name}")
        if path.is_dir():
            yield from _walk(path, name + "/", declared_files=declared_files)
        elif path.is_file():
            yield name, path
        else:
            raise ValueError(f"Expected a regular component file: {name}")


def read_directory(source, *, allow_template: bool = False) -> tuple[dict, dict[str, bytes]]:
    if not source.is_dir() or (isinstance(source, Path) and source.is_symlink()):
        raise ValueError(f"Expected a component directory: {source}")
    header = source.joinpath(_MANIFEST)
    if not header.is_file() or (isinstance(header, Path) and header.is_symlink()):
        raise ValueError(f"Expected a regular component manifest: {header}")
    with header.open("rb") as stream:
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("Component exceeds the byte limit")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise ValueError("Component manifest must be an object")
    template = allow_template and "artifact_digest" not in manifest and "files" not in manifest
    declared = manifest.get("files", {}) if allow_template else None
    if declared is not None and not isinstance(declared, dict):
        raise ValueError("Component files must be an object")
    entries: dict[str, bytes] = {}
    total = len(raw)
    for name, path in _walk(source, declared_files=declared):
        if name == _MANIFEST:
            continue
        allowance = _MAX_BYTES - total
        if len(entries) + 1 >= _MAX_FILES:
            raise ValueError("Component exceeds the file or byte limit")
        with path.open("rb") as stream:
            raw = stream.read(allowance + 1)
        if len(raw) > allowance:
            raise ValueError("Component exceeds the byte limit")
        total += len(raw)
        entries[name] = raw
    return _verified(manifest, entries, template=template)


def _verified(manifest: dict, entries: dict[str, bytes], *, template: bool = False) -> tuple[dict, dict[str, bytes]]:
    actual = {name: sha256(raw) for name, raw in entries.items()}
    if template:
        manifest["files"] = actual
        manifest["artifact_digest"] = artifact_digest(manifest)
    validate_manifest(manifest)
    if actual != manifest["files"]:
        details = sorted(path for path in actual.keys() | manifest["files"].keys()
                         if actual.get(path) != manifest["files"].get(path))
        raise ValueError(f"Component files do not match manifest: {details}")
    if len(canonical(manifest)) + sum(map(len, entries.values())) > _MAX_BYTES:
        raise ValueError("Frozen component exceeds the byte limit")
    return manifest, entries


def _read_archive(archive: Path) -> tuple[dict, dict[str, bytes]]:
    try:
        with ZipFile(archive) as zipped:
            infos = zipped.infolist()
            if len(infos) > _MAX_FILES or sum(entry.file_size for entry in infos) > _MAX_BYTES:
                raise ValueError("Component ZIP exceeds the file or byte limit")
            entries: dict[str, bytes] = {}
            names: set[str] = set()
            for info in infos:
                name = relative_file(info.orig_filename.removesuffix("/") if info.is_dir() else info.orig_filename)
                kind = stat.S_IFMT(info.external_attr >> 16)
                expected_kind = stat.S_IFDIR if info.is_dir() else stat.S_IFREG
                if name.casefold() in names or kind not in {0, expected_kind}:
                    raise ValueError(f"Duplicate or unsupported ZIP member: {name}")
                names.add(name.casefold())
                if not info.is_dir():
                    entries[name] = zipped.read(info)
    except (BadZipFile, NotImplementedError, RuntimeError) as error:
        raise ValueError(f"Cannot open component ZIP {archive}: {error}") from error
    if _MANIFEST not in entries:
        raise ValueError("Missing component manifest.json")
    return _verified(json.loads(entries.pop(_MANIFEST)), entries)


def read_component(source: str | Path) -> tuple[dict, dict[str, bytes]]:
    """Resolve explicit builtins, local paths, and unambiguous bare builtin names."""
    name = str(source)
    if name.startswith("builtin:"):
        return _builtin(name.removeprefix("builtin:"))
    path = Path(source).expanduser()
    if path.is_dir():
        return read_directory(path, allow_template=True)
    if path.is_file():
        return _read_archive(path)
    if name == path.name and _resources().joinpath(name, _MANIFEST).is_file():
        return _builtin(name)
    raise ValueError(f"Component source not found: {source}. Use builtin:<id>, a directory or a ZIP.")


def _pack(manifest: dict, entries: dict[str, bytes], output: Path) -> Release:
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output.parent, prefix=".component-", delete=False) as temporary:
        path = Path(temporary.name)
    try:
        with ZipFile(path, mode="w") as zipped:
            for name, raw in sorted({**entries, _MANIFEST: canonical(manifest)}.items()):
                info = ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                zipped.writestr(info, raw, compress_type=ZIP_DEFLATED)
        os.replace(path, output)
    finally:
        path.unlink(missing_ok=True)
    return Release(manifest, output)


def pack(source: str | Path, output: str | Path) -> Release:
    """Freeze a source directory containing a template manifest and its payload files."""
    source, output = Path(source).absolute(), Path(output).resolve()
    if output.is_relative_to(source.resolve()):
        raise ValueError("Component ZIP must be written outside its source directory")
    manifest, entries = read_directory(source, allow_template=True)
    return _pack(manifest, entries, output)


def pack_builtin(component_id: str, output: str | Path, *, form: str | None = None) -> Release:
    manifest, entries = _builtin(component_id)
    if form is not None and form != manifest["form"]:
        raise ValueError(f"{component_id} is shipped as {manifest['form']}, requested {form}")
    output = Path(output).resolve()
    resources = _resources()
    if isinstance(resources, Path) and output.is_relative_to(resources.resolve()):
        raise ValueError("Component ZIP must be written outside the installed resource directory")
    return _pack(manifest, entries, output)


@lru_cache(maxsize=64)
def _builtin_digest(component_id: str) -> str:
    manifest, _ = _builtin(component_id)
    return manifest["artifact_digest"]


def builtin_policy_assets(component_id: str) -> dict[str, bytes]:
    """Return the bytes and required notices for a declarative candidate's policy."""
    if component_id not in _CONFIGURABLE_POLICIES:
        raise ValueError(f"Unknown built-in policy: {component_id!r}")
    _, files = _builtin(component_id)
    return {name: raw for name, raw in files.items()
            if name == "payload/policy.py" or name.startswith("LICENSE") or name == "provenance.json"}


def builtin_policy_bytes(component_id: str) -> bytes:
    return builtin_policy_assets(component_id)["payload/policy.py"]


@lru_cache(maxsize=2)
def _builtin_policy_hash(component_id: str) -> str:
    return sha256(builtin_policy_bytes(component_id))


def ships_trusted_python(release: Release) -> bool:
    if release.manifest.get("engine") != "python":
        return False
    strategy = release.manifest.get("builtin_policy")
    if strategy in _CONFIGURABLE_POLICIES:
        entrypoint = release.manifest["entrypoint"]
        return release.manifest["files"][entrypoint] == _builtin_policy_hash(strategy)
    return (_resources().joinpath(release.component_id, _MANIFEST).is_file()
            and release.digest == _builtin_digest(release.component_id))
