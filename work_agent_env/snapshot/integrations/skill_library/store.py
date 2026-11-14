"""Transactional Markdown snapshots, isolated candidate branches and CAS publication."""
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
from collections import Counter

from integrations.benchmark_tasks.contracts import digest, nonempty
from integrations.skill_generator.vendor.reward_toolchain import SkillRepo, skill_content


class SkillConflict(ValueError):
    pass


class SkillLibrary:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "skills.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots(version TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS heads(channel TEXT PRIMARY KEY, version TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS publications(candidate TEXT PRIMARY KEY, channel TEXT NOT NULL);
            """)
        self.snapshot({})

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self, skills):
        for name, content in skills.items():
            skill_content(name, content)
        repo = SkillRepo(skills)
        version, body = repo.digest(), repo.canonical_bytes().decode()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?)", (version, body))
        return version

    def get(self, version):
        with self.connect() as db:
            row = db.execute("SELECT body FROM snapshots WHERE version=?", (version,)).fetchone()
        if row is None:
            raise KeyError(version)
        raw = json.loads(row[0])
        skills = {item["name"]: skill_content(item["name"], item["content"]) for item in raw["skills"]}
        if SkillRepo(skills).digest() != version:
            raise ValueError("Skill snapshot identity mismatch")
        return skills

    def head(self, channel="main"):
        nonempty(channel, "channel")
        with self.connect() as db:
            row = db.execute("SELECT version FROM heads WHERE channel=?", (channel,)).fetchone()
        return row[0] if row else None

    @property
    def empty_version(self):
        return SkillRepo().digest()

    def candidate(self, *, parent, skills, provenance, operations):
        self.get(parent)
        if not isinstance(provenance, dict) or not provenance.get("experience_id") or not provenance.get("generator_policy_id"):
            raise ValueError("Candidate needs experience and Generator policy identities")
        version = self.snapshot(skills)
        body = {"schema_version": "workagent-skill-candidate-v1", "parent": parent, "version": version,
                "provenance": provenance, "operations": operations, "training_admitted": False}
        identity = digest(body)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO candidates VALUES(?,?)", (identity, json.dumps(body, ensure_ascii=False, allow_nan=False)))
        return {"id": identity, **body}

    def get_candidate(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT body FROM candidates WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        body = json.loads(row[0])
        if digest(body) != identity:
            raise ValueError("Candidate identity mismatch")
        self.get(body["version"])
        return {"id": identity, **body}

    def publish(self, candidate_id, *, expected_head, channel="main"):
        nonempty(channel, "channel")
        candidate = self.get_candidate(candidate_id)
        parent = expected_head or self.empty_version
        if candidate["parent"] != parent:
            raise SkillConflict("Candidate parent differs from expected published head")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            published = db.execute("SELECT channel FROM publications WHERE candidate=?", (candidate_id,)).fetchone()
            if published:
                if published[0] != channel:
                    raise SkillConflict("Candidate already published to another channel")
                return candidate["version"]
            row = db.execute("SELECT version FROM heads WHERE channel=?", (channel,)).fetchone()
            if (row[0] if row else None) != expected_head:
                raise SkillConflict("Published head changed")
            db.execute("INSERT INTO heads VALUES(?,?) ON CONFLICT(channel) DO UPDATE SET version=excluded.version", (channel, candidate["version"]))
            db.execute("INSERT INTO publications VALUES(?,?)", (candidate_id, channel))
        return candidate["version"]

    def retrieve(self, query, *, version=None, top_k=5):
        if not isinstance(query, str) or type(top_k) is not int or top_k < 1:
            raise ValueError("Invalid retrieval request")
        version = version or self.head() or self.empty_version
        skills = self.get(version)
        # BM25 with the audited SkillOS whitespace tokenizer; deterministic name tie-break.
        corpus = {name: (name + " " + content).lower().split() for name, content in skills.items()}
        if not corpus:
            return []
        average = sum(map(len, corpus.values())) / len(corpus)
        terms = set(query.lower().split())
        frequency = {term: sum(term in words for words in corpus.values()) for term in terms}
        rows = []
        for name, words in corpus.items():
            counts, score = Counter(words), 0.0
            for term in terms:
                tf = counts[term]
                idf = math.log(1 + (len(corpus) - frequency[term] + 0.5) / (frequency[term] + 0.5))
                score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * len(words) / average))
            rows.append({"name": name, "content": skills[name], "score": score, "version": version})
        return sorted(rows, key=lambda row: (-row["score"], row["name"]))[:top_k]
