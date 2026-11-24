"""Install and resolve independent strategy releases without executing their contents."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from uuid import uuid4

from ._local import Database
from .archive import (
    builtin_policy_assets, builtin_policy_bytes, pack, pack_builtin, read_component, read_directory,
    ships_trusted_python,
)
from .manifest import Release, canonical, sha256, stable_id, validate_manifest

__all__ = ["ComponentRegistry", "builtin_policy_assets", "builtin_policy_bytes", "pack", "pack_builtin"]


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
        verified, _ = read_directory(release.path)
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
        """Install a wheel-provided component, a component directory or a release ZIP."""
        manifest, entries = read_component(source)
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
                    raise ValueError(f"{release.component_id}@{release.version} ({release.form}) is already "
                                     "installed with different content. Publish changes under a new version.")
                self._verify(current)
                if not current.trusted_python and ships_trusted_python(current):
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
                    for name, raw in {**entries, "manifest.json": canonical(manifest)}.items():
                        destination = root / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with destination.open("wb") as stream:
                            stream.write(raw)
                            stream.flush()
                            os.fsync(stream.fileno())
                    root.rename(release.path)
            connection.execute("INSERT INTO releases VALUES (?, ?, ?, ?, ?)",
                               (*key, release.digest, canonical(manifest).decode()))
            if ships_trusted_python(release):
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
