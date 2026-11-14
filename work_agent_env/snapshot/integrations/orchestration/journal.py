"""Durable per-batch leases; uncertain calls and optimizer updates are never auto-replayed."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from integrations.benchmark_tasks.contracts import digest


class StateConflict(ValueError):
    pass


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / "journal.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, identity TEXT NOT NULL,
                    state TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job TEXT NOT NULL, state TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, identity):
        key = digest(identity)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE id=?", (key,)).fetchone():
                raise StateConflict("Job already exists; inspect its receipt instead of replaying it")
            db.execute("INSERT INTO jobs VALUES(?,?,?,?)", (key, json.dumps(identity, sort_keys=True), "claimed", "{}"))
            db.execute("INSERT INTO events(job,state,observed_at,payload) VALUES(?,?,?,?)",
                (key, "claimed", datetime.now(timezone.utc).isoformat(), "{}"))
        return key

    def transition(self, key, *, expected, state, payload=None):
        body = json.dumps(payload or {}, ensure_ascii=False, allow_nan=False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM jobs WHERE id=?", (key,)).fetchone()
            if row is None or row[0] not in expected:
                raise StateConflict("Job state changed or transition was replayed")
            db.execute("UPDATE jobs SET state=?,payload=? WHERE id=?", (state, body, key))
            db.execute("INSERT INTO events(job,state,observed_at,payload) VALUES(?,?,?,?)",
                (key, state, datetime.now(timezone.utc).isoformat(), body))

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT identity,state,payload FROM jobs WHERE id=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return {"id": key, "identity": json.loads(row[0]), "state": row[1], "payload": json.loads(row[2])}
