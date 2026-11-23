"""Local transactions shared by artifact ownership and project bindings."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    component TEXT NOT NULL, version TEXT NOT NULL, form TEXT NOT NULL,
    digest TEXT NOT NULL, manifest TEXT NOT NULL,
    PRIMARY KEY (component, version, form)
);
CREATE TABLE IF NOT EXISTS artifact_references (
    owner TEXT NOT NULL, component TEXT NOT NULL, version TEXT NOT NULL, form TEXT NOT NULL,
    digest TEXT NOT NULL, details TEXT NOT NULL,
    PRIMARY KEY (owner, component, version, form),
    FOREIGN KEY (component, version, form) REFERENCES releases(component, version, form)
);
CREATE INDEX IF NOT EXISTS artifact_reference_release ON artifact_references (component, version, form);
CREATE TABLE IF NOT EXISTS validation_reports (
    component TEXT NOT NULL, version TEXT NOT NULL, form TEXT NOT NULL,
    report_digest TEXT NOT NULL, report TEXT NOT NULL,
    PRIMARY KEY (component, version, form, report_digest)
);
CREATE TABLE IF NOT EXISTS trusted_python_releases (digest TEXT PRIMARY KEY);
"""


def state_root(directory: str | Path | None = None) -> Path:
    if directory is not None:
        return Path(directory).expanduser().resolve()
    configured = os.environ.get("SKILL_RSI_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state").expanduser().resolve() / "skill-rsi"


class Database:
    def __init__(self, directory: str | Path | None, schema: str = ""):
        self.root = state_root(directory)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / "local.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        connection = self.connect()
        try:
            connection.executescript(_SCHEMA + schema)
        finally:
            connection.close()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = False):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
