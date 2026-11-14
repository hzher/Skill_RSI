"""Content-addressed skill versions with separate diagnostic and Generator heads."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from training.skill_contract import TrainingRejected, admit_capture, canonical, validate_skill


class SkillConflict(ValueError):
    pass


class SkillRegistry:
    def __init__(self, directory: Path):
        self.directory = directory
        if directory.is_symlink(): raise ValueError("Skill registry directory must be regular")
        directory.mkdir(parents=True, exist_ok=True)
        self.database = directory / "skills.sqlite3"
        if self.database.is_symlink(): raise ValueError("Skill registry database must be regular")
        with self.connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS versions(namespace TEXT NOT NULL, version TEXT NOT NULL, skill_id TEXT NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(namespace,version));
                CREATE TABLE IF NOT EXISTS heads(namespace TEXT NOT NULL, skill_id TEXT NOT NULL, version TEXT NOT NULL,
                    PRIMARY KEY(namespace,skill_id));
                CREATE TABLE IF NOT EXISTS origins(namespace TEXT NOT NULL, origin_id TEXT NOT NULL, version TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(namespace,origin_id));
            ''')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=3)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _publish(self, skill, namespace, origin_id, expected_head):
        validate_skill(skill)
        if not isinstance(origin_id, str) or not 0 < len(origin_id) <= 200:
            raise ValueError("A bounded immutable origin identity is required")
        body = canonical(skill).decode("utf-8")
        version = hashlib.sha256(body.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT version FROM origins WHERE namespace=? AND origin_id=?", (namespace, origin_id)).fetchone()
            if existing is not None:
                if existing[0] != version: raise SkillConflict("Origin identity was reused for a different skill")
                return version
            head = connection.execute("SELECT version FROM heads WHERE namespace=? AND skill_id=?", (namespace, skill["skill_id"])).fetchone()
            current = head[0] if head else None
            if current != expected_head:
                raise SkillConflict("Skill head changed; read it before publishing a new version")
            connection.execute("INSERT OR IGNORE INTO versions VALUES(?,?,?,?)", (namespace, version, skill["skill_id"], body))
            connection.execute("INSERT INTO heads VALUES(?,?,?) ON CONFLICT(namespace,skill_id) DO UPDATE SET version=excluded.version", (namespace, skill["skill_id"], version))
            connection.execute("INSERT INTO origins VALUES(?,?,?,?)", (namespace, origin_id, version, datetime.now(timezone.utc).isoformat()))
        return version

    def publish_diagnostic(self, skill, *, origin_id, expected_head=None):
        return self._publish(skill, "diagnostic", origin_id, expected_head)

    def publish_capture(self, envelope, key, firewall, *, expected_head=None):
        record = envelope["record"]
        admit_capture(record, envelope["signature"], key, firewall=firewall)
        skill = validate_skill(record.get("generated_skill"))
        if record.get("generated_skill_sha256") != hashlib.sha256(canonical(skill)).hexdigest():
            raise TrainingRejected("Generated skill differs from its signed sampled capture")
        return self._publish(skill, "generator", record["capture"]["capture_id"], expected_head)

    def head(self, namespace, skill_id):
        if namespace not in ("diagnostic", "generator"): raise ValueError("Unknown skill namespace")
        with self.connect() as connection:
            row = connection.execute("SELECT version FROM heads WHERE namespace=? AND skill_id=?", (namespace, skill_id)).fetchone()
        return row[0] if row else None

    def get(self, namespace, version):
        with self.connect() as connection:
            row = connection.execute("SELECT body FROM versions WHERE namespace=? AND version=?", (namespace, version)).fetchone()
        if row is None: raise KeyError(version)
        body = row[0]
        if hashlib.sha256(body.encode()).hexdigest() != version: raise ValueError("Stored skill bytes failed content-address verification")
        return validate_skill(json.loads(body))
