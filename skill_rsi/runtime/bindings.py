"""Project binding negotiation and activation at host-confirmed boundaries."""

from __future__ import annotations

from dataclasses import replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Mapping
import os

from ..components import ComponentRegistry, MODES, Release
from ..components.manifest import canonical, required_capabilities, stable_id
from .model import Binding
from .policy import action_capability
from .store import RuntimeStore, identity


def capabilities_of(description: Mapping) -> frozenset[str]:
    capabilities = description.get("capabilities", ())
    if isinstance(capabilities, Mapping):
        return frozenset(name for name, enabled in capabilities.items() if enabled)
    return frozenset(capabilities)


def supports_action(description: Mapping, boundary: str, kind: str) -> bool:
    capability = action_capability(kind)
    if capability not in capabilities_of(description):
        return False
    nodes = description.get("nodes")
    if not nodes:
        return True
    node = nodes.get(boundary, ())
    return capability in node or kind in node


def _scope_mismatch(release: Release, host: str, mode: str, description: Mapping) -> str | None:
    scope = release.manifest["scope"]
    if host not in scope["hosts"] and "*" not in scope["hosts"]:
        return f"component does not support host {host}"
    if mode not in scope["modes"]:
        return f"component does not support mode {mode}"
    host_versions = scope.get("host_versions", {}).get(host)
    for label, patterns in (("host_version", host_versions), ("adapter_version", scope.get("adapter_versions"))):
        if patterns:
            version = description.get(label)
            if not isinstance(version, str) or not any(fnmatchcase(version, item) for item in patterns):
                return f"{label} {version!r} is outside {patterns}"
    return None


class CapabilityGap(ValueError):
    """The reported host nodes do not yet cover the requested binding."""


def negotiate(release: Release, host: str, mode: str, description: Mapping) -> None:
    if description.get("host", host) != host:
        raise ValueError("Capability receipt belongs to a different host")
    mismatch = _scope_mismatch(release, host, mode, description)
    if mismatch:
        raise ValueError(mismatch)
    manifest = release.manifest
    capabilities = capabilities_of(description)
    required = required_capabilities(manifest, mode)
    kind = "display_suggestion" if mode == "suggest" else manifest["action"]["kind"]
    if mode != "shadow":
        required |= {action_capability(kind)}
    if required - capabilities:
        raise CapabilityGap(f"{host} lacks capabilities: {', '.join(sorted(required - capabilities))}")
    engine = manifest.get("engine", "skill" if release.form == "skill" else "native")
    if engine != "skill" and not release.trusted_python and f"execute_{engine}_component" not in capabilities:
        raise CapabilityGap(f"{host} has no isolated {engine} component executor")
    available = set(description.get("boundaries", ()))
    triggers = set(manifest["trigger"]["boundaries"])
    boundaries = triggers & available if "*" not in triggers else available
    if not boundaries:
        raise CapabilityGap(f"{host} exposes none of the component trigger boundaries: {sorted(triggers)}")
    if mode != "shadow" and not any(supports_action(description, boundary, kind) for boundary in boundaries):
        raise CapabilityGap(f"{host} cannot deliver {kind} at a component trigger boundary")


def _conflict(left: Release, right: Release) -> bool:
    manifest, other = left.manifest, right.manifest
    targets, other_targets = set(manifest["action"]["writes"]), set(other["action"]["writes"])
    boundaries, other_boundaries = (set(value["trigger"]["boundaries"]) for value in (manifest, other))
    if not targets or not other_targets:
        return False
    shared_boundary = boundaries & other_boundaries or "*" in boundaries | other_boundaries
    shared_target = targets & other_targets or "*" in targets | other_targets
    return bool(shared_boundary and shared_target)


def project_identity(project: str | Path) -> str:
    """Stable identity for the native project root after normalization."""
    return "project-" + identity(os.path.normcase(str(Path(project).expanduser().resolve())))


class ProjectBindings:
    def __init__(self, project: str | Path, *, state_dir: str | Path | None = None):
        self.project = Path(project).expanduser().resolve(strict=True)
        if not self.project.is_dir():
            raise ValueError("Project path must be a directory")
        self.project_id = project_identity(self.project)
        self.registry = ComponentRegistry(state_dir)
        self.store = RuntimeStore(self.registry.db.root, self.project_id)
        self.state_dir = self.store.db.root

    def _check_conflicts(self, connection, candidate: Release, host: str, mode: str) -> None:
        if mode != "apply":
            return
        for other in (*self.store.revisions(connection, host), *self.store.revisions(connection, host, effective=True)):
            if not other.enabled or other.activation_error or other.mode != "apply" or other.component_id == candidate.component_id:
                continue
            release = self.registry._resolve(connection, other.component_id, other.version, other.form, verify=False)
            if _conflict(candidate, release):
                raise ValueError(f"Exclusive writes conflict with {other.component_id}@{other.version}")

    def _check_state(self, connection, host: str, release: Release, previous: Binding | None) -> None:
        if previous is not None and previous.state_version == release.manifest.get("state_version"):
            return
        rows = connection.execute(
            """SELECT DISTINCT state_version FROM policy_state
            WHERE project = ? AND host = ? AND component = ? AND namespace = 'live'""",
            (self.project_id, host, release.component_id),
        )
        versions = {row["state_version"] for row in rows}
        if versions and release.manifest.get("state_version") not in versions:
            raise ValueError(f"Incompatible state schema {release.manifest.get('state_version')!r}; "
                             f"preserved existing versions {sorted(versions)}")

    def _change(self, connection, release: Release | None, host: str, component: str, *,
                enabled: bool, mode: str, capabilities: Mapping, expected_revision: int | None,
                options: dict | None) -> Binding:
        head = self.store.head(connection, host, component)
        revision = head["desired"] if head is not None else 0
        if expected_revision is not None and expected_revision != revision:
            raise ValueError(f"Binding revision changed: expected {expected_revision}, current {revision}")
        desired = self.store.binding(connection, host, component, revision)
        effective_revision = head["effective"] if head is not None else None
        active = self.store.binding(connection, host, component, effective_revision)

        if enabled:
            assert release is not None
            if options is not None and not isinstance(options, dict):
                raise ValueError("Binding options must be an object")
            validate_options = {**release.manifest.get("defaults", {}), **(options or {})}
            canonical(validate_options)
            retention = validate_options.get("original_retention_seconds")
            if retention is not None and (type(retention) is not int or not 1 <= retention <= 2**53):
                raise ValueError("original_retention_seconds must be a positive represented integer")
            negotiate(release, host, mode, capabilities)
            self._check_conflicts(connection, release, host, mode)
            self._check_state(connection, host, release, active or desired)
            version, form, digest = release.version, release.form, release.digest
            state = release.manifest.get("state_version")
        else:
            if desired is None:
                raise ValueError(f"No binding to disable: {host}/{component}")
            if not desired.enabled:
                return desired
            version, form, digest = desired.version, desired.form, desired.artifact_digest
            mode, state, validate_options = desired.mode, desired.state_version, desired.options

        if not desired or desired.activation_error or (
            desired.version, desired.form, desired.artifact_digest, desired.enabled, desired.mode, desired.options
        ) != (version, form, digest, enabled, mode, validate_options):
            boundary = capabilities.get("reload_boundary") or (desired.reload_boundary if desired else "any_event")
            binding = Binding(self.project_id, host, component, version, form, digest, enabled, mode,
                              revision + 1, revision or None, boundary, state, validate_options)
            self.store.save_binding(connection, binding)
            self.store.set_head(connection, binding, desired=binding.binding_revision, effective=effective_revision)
            return binding
        return desired

    def enable(self, host: str, component_id: str, version: str, *, form: str | None = None,
               mode: str = "shadow", capabilities: Mapping, expected_revision: int | None = None,
               options: dict | None = None) -> Binding:
        stable_id(host, "host")
        if mode not in MODES:
            raise ValueError(f"Unsupported project mode: {mode}")
        with self.store.db.transaction(write=True) as connection:
            release = self.registry._resolve(connection, component_id, version, form)
            return self._change(connection, release, host, component_id, enabled=True, mode=mode,
                                capabilities=capabilities, expected_revision=expected_revision, options=options)

    def disable(self, host: str, component_id: str, *, capabilities: Mapping,
                expected_revision: int | None = None) -> Binding:
        with self.store.db.transaction(write=True) as connection:
            return self._change(connection, None, host, component_id, enabled=False, mode="shadow",
                                capabilities=capabilities, expected_revision=expected_revision, options=None)

    def _activate(self, connection, host: str, boundary: str, boundary_ref, capabilities: Mapping) -> tuple[Binding, ...]:
        results = []
        for binding in self.store.revisions(connection, host):
            if binding.status != "pending" or binding.reload_boundary not in {"any_event", boundary}:
                continue
            head = self.store.head(connection, host, binding.component_id)
            try:
                if binding.enabled:
                    release = self.registry._resolve(connection, binding.component_id, binding.version, binding.form)
                    if release.digest != binding.artifact_digest:
                        raise ValueError("Pending artifact digest changed")
                    negotiate(release, host, binding.mode, capabilities)
                effective = replace(binding, effective_boundary_ref=boundary_ref,
                                    adapter_version=capabilities.get("adapter_version"),
                                    capability_receipt=dict(capabilities))
            except (OSError, ValueError) as error:
                if isinstance(error, CapabilityGap) and not capabilities.get("complete"):
                    continue
                rejected = replace(binding, activation_error=str(error))
                self.store.save_binding(connection, rejected)
                self.store.set_head(connection, rejected, desired=rejected.binding_revision, effective=head["effective"])
                results.append(rejected)
                continue
            self.store.save_binding(connection, effective)
            self.store.set_head(connection, effective, desired=effective.binding_revision,
                                effective=effective.binding_revision)
            results.append(effective)
        return tuple(results)

    def confirm(self, host: str, boundary: str, boundary_ref, capabilities: Mapping) -> tuple[Binding, ...]:
        if boundary_ref is None:
            raise ValueError("Actual activation requires a host boundary reference")
        with self.store.db.transaction(write=True) as connection:
            return self._activate(connection, host, boundary, boundary_ref, capabilities)

    def bindings(self, host: str | None = None) -> tuple[Binding, ...]:
        with self.store.db.transaction() as connection:
            hosts = (host,) if host is not None else tuple(row[0] for row in connection.execute(
                "SELECT DISTINCT host FROM binding_heads WHERE project = ? ORDER BY host", (self.project_id,),
            ))
            return tuple(binding for name in hosts for binding in self.store.revisions(connection, name))

    def set_observe(self, host: str, enabled: bool = True) -> bool:
        with self.store.db.transaction(write=True) as connection:
            connection.execute(
                """INSERT INTO observe_settings VALUES (?, ?, ?)
                ON CONFLICT(project, host) DO UPDATE SET enabled = excluded.enabled""",
                (self.project_id, stable_id(host, "host"), bool(enabled)),
            )
        return bool(enabled)

    def is_observing(self, host: str) -> bool:
        with self.store.db.transaction() as connection:
            row = connection.execute("SELECT enabled FROM observe_settings WHERE project = ? AND host = ?",
                                     (self.project_id, host)).fetchone()
        return bool(row and row[0])

    def status(self, host: str) -> dict:
        with self.store.db.transaction() as connection:
            desired = self.store.revisions(connection, host)
            effective = self.store.revisions(connection, host, effective=True)
        return {
            "project": str(self.project), "project_id": self.project_id, "host": host,
            "observe": self.is_observing(host),
            "bindings": [binding.to_dict() for binding in desired],
            "effective_bindings": [binding.to_dict() for binding in effective],
        }
