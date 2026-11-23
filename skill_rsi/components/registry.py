"""Install and resolve independent strategy releases without executing their contents."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files as resource_files
from pathlib import Path
from tempfile import TemporaryDirectory, NamedTemporaryFile
import json
import os
import shutil
import stat
from uuid import uuid4
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from ._local import Database
from .manifest import Release, artifact_digest, canonical, relative_file, sha256, stable_id, validate_manifest

_MANIFEST = "manifest.json"
_MAX_BYTES = 128 * 1024 * 1024
_MAX_FILES = 2048
_BUILTINS = frozenset({"tool-result-view", "constraint-check"})


def _walk(source, prefix: str = "", *, packaging: bool = False):
    for path in sorted(source.iterdir(), key=lambda item: item.name):
        if packaging and (path.name == "__pycache__" or path.name.endswith((".pyc", ".pyo"))):
            continue
        name = relative_file(prefix + path.name)
        if isinstance(path, Path) and path.is_symlink():
            raise ValueError(f"Symbolic link in component: {name}")
        if path.is_dir():
            yield from _walk(path, name + "/", packaging=packaging)
        elif path.is_file():
            yield name, path
        else:
            raise ValueError(f"Expected a regular component file: {name}")


def _read_tree(source, *, packaging: bool = False) -> dict[str, bytes]:
    if isinstance(source, Path) and source.is_symlink():
        raise ValueError("A component root must be a real directory")
    entries: dict[str, bytes] = {}
    total = 0
    for name, path in _walk(source, packaging=packaging):
        allowance = _MAX_BYTES - total
        if len(entries) == _MAX_FILES or allowance < 0:
            raise ValueError("Component exceeds the file or byte limit")
        with path.open("rb") as stream:
            raw = stream.read(allowance + 1)
        if len(raw) > allowance:
            raise ValueError("Component exceeds the byte limit")
        total += len(raw)
        entries[name] = raw
    return entries


def _verified(entries: dict[str, bytes]) -> tuple[dict, dict[str, bytes]]:
    try:
        manifest = validate_manifest(json.loads(entries.pop(_MANIFEST)))
    except KeyError as error:
        raise ValueError("Missing component manifest.json") from error
    actual = {name: sha256(raw) for name, raw in entries.items()}
    if actual != manifest["files"]:
        details = sorted(path for path in actual.keys() | manifest["files"].keys()
                         if actual.get(path) != manifest["files"].get(path))
        raise ValueError(f"Component files do not match manifest: {details}")
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
                name = relative_file(info.filename.removesuffix("/") if info.is_dir() else info.filename)
                kind = stat.S_IFMT(info.external_attr >> 16)
                if name.casefold() in names or kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ValueError(f"Duplicate or unsupported ZIP member: {name}")
                names.add(name.casefold())
                if not info.is_dir():
                    entries[name] = zipped.read(info)
    except (BadZipFile, NotImplementedError, RuntimeError) as error:
        raise ValueError(f"Cannot open component ZIP {archive}: {error}") from error
    return _verified(entries)


def _freeze(source) -> tuple[dict, dict[str, bytes]]:
    entries = _read_tree(source, packaging=True)
    try:
        manifest = json.loads(entries.pop(_MANIFEST))
    except KeyError as error:
        raise ValueError("Missing source manifest.json") from error
    if not isinstance(manifest, dict):
        raise ValueError("Source manifest must be an object")
    manifest["files"] = {name: sha256(data) for name, data in entries.items()}
    manifest["artifact_digest"] = artifact_digest(manifest)
    validate_manifest(manifest)
    return manifest, entries


def _pack(source, output: Path) -> Release:
    manifest, entries = _freeze(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output.parent, prefix=".component-", delete=False) as temporary:
        path = Path(temporary.name)
    try:
        with ZipFile(path, mode="w") as zipped:
            for name, raw in sorted({**entries, _MANIFEST: canonical(manifest)}.items()):
                info = ZipInfo(name)
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
    return _pack(source, output)


def pack_builtin(component_id: str, output: str | Path, *, form: str | None = None) -> Release:
    stable_id(component_id, "component_id")
    root = resource_files("skill_rsi.components").joinpath("resources", component_id)
    if not root.is_dir():
        raise ValueError(f"Unknown built-in component: {component_id}")
    manifest = json.loads(root.joinpath(_MANIFEST).read_text(encoding="utf-8"))
    if form is not None and form != manifest["form"]:
        raise ValueError(f"{component_id} is shipped as {manifest['form']}, requested {form}")
    return _pack(root, Path(output).resolve())


@lru_cache(maxsize=2)
def _builtin_digest(component_id: str) -> str:
    root = resource_files("skill_rsi.components").joinpath("resources", component_id)
    manifest, _ = _freeze(root)
    return manifest["artifact_digest"]


def builtin_policy_assets(component_id: str) -> dict[str, bytes]:
    """Return the bytes and required notices for a declarative candidate's policy."""
    if component_id not in _BUILTINS:
        raise ValueError(f"Unknown built-in policy: {component_id!r}")
    root = resource_files("skill_rsi.components").joinpath("resources", component_id)
    files = _read_tree(root, packaging=True)
    return {name: raw for name, raw in files.items()
            if name == "payload/policy.py" or name.startswith("LICENSE") or name == "provenance.json"}


def builtin_policy_bytes(component_id: str) -> bytes:
    return builtin_policy_assets(component_id)["payload/policy.py"]


@lru_cache(maxsize=2)
def _builtin_policy_hash(component_id: str) -> str:
    return sha256(builtin_policy_bytes(component_id))


def _ships_trusted_python(release: Release) -> bool:
    if release.manifest.get("engine") != "python":
        return False
    strategy = release.manifest.get("builtin_policy")
    if strategy in _BUILTINS:
        entrypoint = release.manifest["entrypoint"]
        return release.manifest["files"][entrypoint] == _builtin_policy_hash(strategy)
    return release.component_id in _BUILTINS and release.digest == _builtin_digest(release.component_id)


class ComponentRegistry:
    def __init__(self, state_dir: str | Path | None = None):
        self.db = Database(state_dir)
        self.root = self.db.root / "artifacts"

    def _path(self, manifest: dict) -> Path:
        path = self.root / manifest["component_id"] / manifest["version"] / manifest["form"]
        node = path
        while node != self.db.root:
            if node.is_symlink():
                raise ValueError(f"Symbolic link in managed artifact path: {node}")
            node = node.parent
        return path

    def _row_release(self, connection, row) -> Release:
        manifest = validate_manifest(json.loads(row["manifest"]))
        if manifest["artifact_digest"] != row["digest"]:
            raise ValueError("Component catalog digest mismatch")
        trusted = connection.execute("SELECT 1 FROM trusted_python_releases WHERE digest = ?", (row["digest"],)).fetchone()
        return Release(manifest, self._path(manifest), trusted_python=trusted is not None)

    def _verify(self, release: Release) -> None:
        verified, _ = _verified(_read_tree(release.path))
        if verified != release.manifest:
            raise ValueError(f"Installed artifact differs from catalog: {release.path}")

    def _resolve(self, connection, component_id: str, version: str, form: str | None = None,
                 *, verify: bool = True) -> Release:
        key = (stable_id(component_id), stable_id(version, "component version"))
        sql = "SELECT * FROM releases WHERE component = ? AND version = ?"
        if form is not None:
            key += (stable_id(form, "component form"),)
            sql += " AND form = ?"
        rows = connection.execute(sql, key).fetchall()
        if not rows:
            raise ValueError(f"Component is not installed: {component_id}@{version}")
        if len(rows) > 1:
            raise ValueError(f"Select the form for {component_id}@{version}: {[row['form'] for row in rows]}")
        release = self._row_release(connection, rows[0])
        if verify:
            self._verify(release)
        return release

    def resolve(self, component_id: str, version: str, form: str | None = None) -> Release:
        with self.db.transaction() as connection:
            return self._resolve(connection, component_id, version, form)

    def list(self) -> tuple[Release, ...]:
        with self.db.transaction() as connection:
            rows = connection.execute("SELECT * FROM releases ORDER BY component, version, form")
            return tuple(self._row_release(connection, row) for row in rows)

    def install(self, source: str | Path) -> Release:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ValueError(f"Component ZIP not found: {path}")
        manifest, entries = _read_archive(path)
        release = Release(manifest, self._path(manifest))
        key = release.component_id, release.version, release.form
        with self.db.transaction(write=True) as connection:
            existing = connection.execute(
                """SELECT * FROM releases
                WHERE lower(component) = lower(?) AND lower(version) = lower(?) AND form = ?""", key,
            ).fetchone()
            if existing is not None:
                current = self._row_release(connection, existing)
                if current.digest != release.digest or current.manifest != release.manifest:
                    raise ValueError(f"Conflicting immutable component release: {key}")
                self._verify(current)
                if not current.trusted_python and _ships_trusted_python(current):
                    self._mark_trusted(connection, current)
                    return Release(current.manifest, current.path, trusted_python=True)
                return current
            release.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if release.path.exists():
                # A complete directory can survive an interrupted database commit.
                self._verify(release)
            else:
                with TemporaryDirectory(dir=release.path.parent, prefix=".install-") as staging:
                    root = Path(staging) / "release"
                    root.mkdir(mode=0o700)
                    for name, raw in {**entries, _MANIFEST: canonical(manifest)}.items():
                        destination = root / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with destination.open("wb") as stream:
                            stream.write(raw)
                            stream.flush()
                            os.fsync(stream.fileno())
                    root.rename(release.path)
            connection.execute("INSERT INTO releases VALUES (?, ?, ?, ?, ?)",
                               (*key, release.digest, canonical(manifest).decode()))
            if _ships_trusted_python(release):
                self._mark_trusted(connection, release)
                return Release(manifest, release.path, trusted_python=True)
        return release

    @staticmethod
    def _mark_trusted(connection, release: Release) -> None:
        connection.execute("INSERT OR IGNORE INTO trusted_python_releases VALUES (?)", (release.digest,))

    def _references(self, connection, release: Release) -> list[dict]:
        rows = connection.execute(
            """SELECT details FROM artifact_references
            WHERE component = ? AND version = ? AND form = ? AND digest = ? ORDER BY owner""",
            (release.component_id, release.version, release.form, release.digest),
        )
        return [json.loads(row["details"]) for row in rows]

    def references(self, release: Release) -> list[dict]:
        with self.db.transaction() as connection:
            return self._references(connection, release)

    def uninstall(self, component_id: str, version: str, form: str | None = None) -> Release:
        retired = None
        try:
            with self.db.transaction(write=True) as connection:
                release = self._resolve(connection, component_id, version, form, verify=False)
                references = self._references(connection, release)
                if references:
                    raise ValueError(f"Component is still in use: {references}")
                if release.path.exists():
                    retired = release.path.with_name(f".removed-{uuid4().hex}")
                    release.path.rename(retired)
                connection.execute("DELETE FROM releases WHERE component = ? AND version = ? AND form = ?",
                                   (release.component_id, release.version, release.form))
        except BaseException:
            if retired is not None and retired.exists():
                retired.rename(release.path)
            raise
        if retired is not None:
            shutil.rmtree(retired)
        return release

    def record_report(self, release: Release, report: dict) -> str:
        """Register a later validation report without changing the artifact digest."""
        if report.get("artifact_digest") != release.digest:
            raise ValueError("Validation report must cite the exact artifact digest")
        if report.get("status") not in {"validated_in_scope", "inconclusive", "rejected"}:
            raise ValueError("Invalid validation report status")
        raw = canonical(report)
        digest = sha256(raw)
        with self.db.transaction(write=True) as connection:
            current = self._resolve(connection, release.component_id, release.version, release.form)
            if current.digest != release.digest:
                raise ValueError("Validation report refers to a different release")
            connection.execute("INSERT OR IGNORE INTO validation_reports VALUES (?, ?, ?, ?, ?)",
                               (release.component_id, release.version, release.form, digest, raw.decode()))
        return digest

    def reports(self, release: Release) -> list[dict]:
        with self.db.transaction() as connection:
            rows = connection.execute(
                """SELECT report FROM validation_reports
                WHERE component = ? AND version = ? AND form = ? ORDER BY report_digest""",
                (release.component_id, release.version, release.form),
            )
            return [report for row in rows
                    if (report := json.loads(row["report"])).get("artifact_digest") == release.digest]
