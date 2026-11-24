"""Transactional binding history, operation ledger and isolated local policy state."""

from __future__ import annotations

from dataclasses import asdict
import json

from ..components._local import Database
from ..components.manifest import canonical, sha256
from .model import Binding, Decision

SCHEMA = """
CREATE TABLE IF NOT EXISTS binding_revisions (
    project TEXT NOT NULL, host TEXT NOT NULL, component TEXT NOT NULL, revision INTEGER NOT NULL,
    binding TEXT NOT NULL, PRIMARY KEY (project, host, component, revision)
);
CREATE TABLE IF NOT EXISTS binding_heads (
    project TEXT NOT NULL, host TEXT NOT NULL, component TEXT NOT NULL,
    desired INTEGER NOT NULL, effective INTEGER,
    PRIMARY KEY (project, host, component)
);
CREATE TABLE IF NOT EXISTS configured_extensions (
    project TEXT NOT NULL, host TEXT NOT NULL, component TEXT NOT NULL,
    PRIMARY KEY (project, host, component)
);
CREATE TABLE IF NOT EXISTS runtime_sessions (
    project TEXT NOT NULL, host TEXT NOT NULL, session TEXT NOT NULL,
    bindings TEXT NOT NULL, PRIMARY KEY (project, host, session)
);
CREATE TABLE IF NOT EXISTS observe_settings (
    project TEXT NOT NULL, host TEXT NOT NULL, enabled INTEGER NOT NULL,
    PRIMARY KEY (project, host)
);
CREATE TABLE IF NOT EXISTS runtime_events (
    event_key TEXT PRIMARY KEY, project TEXT NOT NULL, host TEXT NOT NULL, session TEXT NOT NULL,
    fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_decisions (
    operation TEXT PRIMARY KEY, event_key TEXT NOT NULL REFERENCES runtime_events(event_key),
    project TEXT NOT NULL, host TEXT NOT NULL, session TEXT NOT NULL, component TEXT NOT NULL,
    version TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL, decision TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_events ON runtime_decisions(event_key);
CREATE INDEX IF NOT EXISTS decisions_scope ON runtime_decisions(project, host, session, component);
CREATE TABLE IF NOT EXISTS suggestion_effects (
    operation TEXT PRIMARY KEY, parent TEXT NOT NULL UNIQUE REFERENCES runtime_decisions(operation)
);
CREATE TABLE IF NOT EXISTS policy_state (
    project TEXT NOT NULL, host TEXT NOT NULL, component TEXT NOT NULL,
    lineage TEXT NOT NULL, namespace TEXT NOT NULL, state_version TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (project, host, component, lineage, namespace, state_version)
);
CREATE TABLE IF NOT EXISTS tool_originals (
    project TEXT NOT NULL, handle TEXT NOT NULL, content BLOB NOT NULL, digest TEXT NOT NULL,
    source_ref TEXT NOT NULL, expires REAL, PRIMARY KEY (project, handle)
);
CREATE TABLE IF NOT EXISTS policy_state_versions (
    operation TEXT PRIMARY KEY, project TEXT NOT NULL, host TEXT NOT NULL, component TEXT NOT NULL,
    lineage TEXT NOT NULL, namespace TEXT NOT NULL, state_version TEXT NOT NULL,
    parent_digest TEXT NOT NULL, value_digest TEXT NOT NULL, value TEXT NOT NULL
);
"""


def json_text(value) -> str:
    return canonical(value).decode("utf-8")


def identity(value) -> str:
    return sha256(canonical(value))


def reference(connection, owner: str, binding: Binding, details: dict) -> None:
    connection.execute(
        """INSERT OR REPLACE INTO artifact_references (owner, component, version, form, digest, details)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (owner, binding.component_id, binding.version, binding.form, binding.artifact_digest, json_text(details)),
    )


def forget_reference(connection, owner: str) -> None:
    connection.execute("DELETE FROM artifact_references WHERE owner = ?", (owner,))


def save_decision(connection, event_key: str, value: Decision) -> None:
    connection.execute(
        """INSERT INTO runtime_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(operation) DO UPDATE SET stage = excluded.stage, status = excluded.status,
            decision = excluded.decision""",
        (value.operation_key, event_key, value.project_id, value.host, value.session_id,
         value.component_id, value.version, value.stage, value.status, json_text(asdict(value))),
    )


def decision_from(row) -> Decision:
    value = json.loads(row["decision"])
    value["receipts"] = tuple(value["receipts"])
    return Decision(**value)


class RuntimeStore:
    def __init__(self, directory, project_id: str):
        self.db = Database(directory, SCHEMA)
        self.project_id = project_id

    def revisions(self, connection, host: str, *, effective: bool = False) -> tuple[Binding, ...]:
        column = "effective" if effective else "desired"
        rows = connection.execute(
            f"""SELECT r.binding FROM binding_heads h JOIN binding_revisions r
            ON r.project = h.project AND r.host = h.host AND r.component = h.component
            AND r.revision = h.{column} WHERE h.project = ? AND h.host = ? ORDER BY h.component""",
            (self.project_id, host),
        )
        return tuple(Binding(**json.loads(row["binding"])) for row in rows)

    def head(self, connection, host: str, component: str):
        return connection.execute(
            "SELECT * FROM binding_heads WHERE project = ? AND host = ? AND component = ?",
            (self.project_id, host, component),
        ).fetchone()

    def binding(self, connection, host: str, component: str, revision: int | None) -> Binding | None:
        if revision is None:
            return None
        row = connection.execute(
            """SELECT binding FROM binding_revisions WHERE project = ? AND host = ? AND component = ?
            AND revision = ?""", (self.project_id, host, component, revision),
        ).fetchone()
        return Binding(**json.loads(row["binding"])) if row is not None else None

    def save_binding(self, connection, binding: Binding) -> None:
        connection.execute(
            """INSERT INTO binding_revisions VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project, host, component, revision) DO UPDATE SET binding = excluded.binding""",
            (binding.project_id, binding.host, binding.component_id, binding.binding_revision, json_text(asdict(binding))),
        )

    def set_head(self, connection, binding: Binding, *, desired: int, effective: int | None) -> None:
        connection.execute(
            """INSERT INTO binding_heads VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project, host, component) DO UPDATE SET desired = excluded.desired,
                effective = excluded.effective""",
            (binding.project_id, binding.host, binding.component_id, desired, effective),
        )
        owner = self.project_id, binding.host, binding.component_id
        for name, revision in (("desired", desired), ("effective", effective)):
            key = identity([name, *owner])
            forget_reference(connection, key)
            current = self.binding(connection, binding.host, binding.component_id, revision)
            if current is not None and current.enabled and current.activation_error is None:
                reference(connection, key, current, {
                    "kind": name, "project_id": self.project_id, "host": binding.host,
                    "binding_revision": current.binding_revision,
                })

    def session_bindings(self, connection, host: str, session: str) -> tuple[Binding, ...] | None:
        row = connection.execute(
            "SELECT bindings FROM runtime_sessions WHERE project = ? AND host = ? AND session = ?",
            (self.project_id, host, session),
        ).fetchone()
        if row is None:
            return None
        return tuple(Binding(**value) for value in json.loads(row["bindings"]))

    def open_session(self, connection, host: str, session: str, bindings: tuple[Binding, ...]) -> None:
        connection.execute(
            "INSERT INTO runtime_sessions VALUES (?, ?, ?, ?)",
            (self.project_id, host, session, json_text([asdict(value) for value in bindings])),
        )
        for binding in bindings:
            if binding.enabled:
                reference(connection, identity(["session", self.project_id, host, session, binding.component_id]),
                          binding, {"kind": "session", "project_id": self.project_id,
                                    "host": host, "session_id": session,
                                    "binding_revision": binding.binding_revision})

    def close_session(self, connection, host: str, session: str) -> None:
        current = self.session_bindings(connection, host, session)
        for binding in current or ():
            forget_reference(connection, identity(["session", self.project_id, host, session, binding.component_id]))
        connection.execute(
            "DELETE FROM runtime_sessions WHERE project = ? AND host = ? AND session = ?",
            (self.project_id, host, session),
        )
