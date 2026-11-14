"""Immutable execution evidence with content-addressed artifacts and curator views."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3

from integrations.benchmark_tasks.contracts import digest, strict_json, validate_task


class ExperienceStore:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "experience.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS blobs(sha256 TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS episodes(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS views(id TEXT PRIMARY KEY, episode TEXT NOT NULL, body TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def capture(self, *, task, receipt, trajectory, directory, executor, calls=None):
        validate_task(task)
        if receipt.get("task_sha256") != digest(task) or receipt.get("task_uid") != task["uid"]:
            raise ValueError("Experience task identity differs from executor receipt")
        directory = Path(directory)
        executor = deepcopy(executor)
        plan_path = Path(executor["plan_ref"]) if executor.get("plan_ref") else None
        if plan_path is not None and plan_path.is_file():
            plan_bytes = plan_path.read_bytes()
            if hashlib.sha256(plan_bytes).hexdigest() != executor.get("plan_sha256"):
                raise ValueError("Execution plan identity changed during experience capture")
            executor["plan"] = strict_json(plan_bytes.decode("utf-8"))
        blobs, artifacts = {}, []
        workspace = directory / "workspace"
        for path in sorted(workspace.rglob("*")) if workspace.is_dir() else []:
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(workspace.resolve()):
                continue
            body = path.read_bytes()
            sha = hashlib.sha256(body).hexdigest()
            blobs[sha] = body
            artifacts.append({"path": path.relative_to(workspace).as_posix(), "sha256": sha, "bytes": len(body)})
        usage = [deepcopy(row["usage"]) for row in trajectory if row.get("usage") is not None]
        costs = {"usage_by_response": usage, "model_requests_attempted": receipt.get("model_requests_attempted", 0),
                 "http_requests_attempted": receipt.get("http_requests_attempted", 0),
                 "duration_seconds": receipt.get("duration_seconds"), "money": None,
                 "money_status": "not_reported", "usage_complete": len(usage) == receipt.get("model_replies", 0)}
        record = {"schema_version": "workagent-experience-v1", "task": deepcopy(task),
                  "executor": deepcopy(executor), "receipt": deepcopy(receipt),
                  "trajectory": deepcopy(trajectory), "calls": deepcopy(calls or []),
                  "artifacts": artifacts, "costs": costs, "training_admitted": False}
        # Self-references are added only to the runtime receipt after capture.
        record["receipt"].pop("experience", None)
        identity = digest(record)
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO blobs VALUES(?,?)", blobs.items())
            db.execute("INSERT OR IGNORE INTO episodes VALUES(?,?)", (identity, json.dumps(record, ensure_ascii=False, allow_nan=False)))
        view = self.view(identity)
        return {"id": identity, "database": str(self.database), "schema_version": record["schema_version"], "view_id": digest(view)}

    def view(self, identity, **budgets):
        view = curator_view(self.get(identity), **budgets)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO views VALUES(?,?,?)", (digest(view), identity,
                json.dumps(view, ensure_ascii=False, allow_nan=False)))
        return view

    def get(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT body FROM episodes WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        record = json.loads(row[0])
        if digest(record) != identity:
            raise ValueError("Experience content identity mismatch")
        return record

    def artifact(self, sha):
        with self.connect() as db:
            row = db.execute("SELECT body FROM blobs WHERE sha256=?", (sha,)).fetchone()
        if row is None:
            raise KeyError(sha)
        if hashlib.sha256(row[0]).hexdigest() != sha:
            raise ValueError("Artifact content identity mismatch")
        return row[0]


def curator_view(record, *, max_steps=100, observation_chars=12000):
    """Loss is explicit; keep full evidence in store, never put evaluator internals in prompts."""
    if type(max_steps) is not int or max_steps < 1 or type(observation_chars) is not int or observation_chars < 1:
        raise ValueError("Invalid compression budget")
    steps, truncation = [], []
    calls = record["calls"]
    raw_by_id = {call["public_call_id"]: call for call in calls if call.get("public_call_id") is not None}
    if calls and not raw_by_id:
        for i, call in enumerate(calls, 1):
            steps.append({"step": i, "action": json.dumps({"name": call["name"], "arguments": call["arguments"]}, ensure_ascii=False),
                          "observation": json.dumps(call.get("output", {"error": call.get("error")}), ensure_ascii=False)})
        for row in record["trajectory"]:
            if row["role"] == "assistant" and not row.get("calls") and row.get("text"):
                steps.append({"step": len(steps) + 1, "action": "final_response", "observation": row["text"]})
    else:
        pending = {}
        for row in record["trajectory"]:
            if row["role"] == "assistant":
                for call in row.get("calls", []):
                    pending[call["id"]] = call
                if not row.get("calls") and row.get("text"):
                    steps.append({"step": len(steps) + 1, "action": "final_response", "observation": row["text"]})
            elif row["role"] == "tool":
                call = pending.pop(row["id"], {"id": row["id"], "name": "unknown", "arguments": {}})
                raw = raw_by_id.get(row["id"])
                observation = json.dumps(raw.get("output", {"error": raw.get("error")}), ensure_ascii=False) if raw else row["content"]
                steps.append({"step": len(steps) + 1, "action": json.dumps(call, ensure_ascii=False), "observation": observation})
        for call in pending.values():
            steps.append({"step": len(steps) + 1, "action": json.dumps(call, ensure_ascii=False), "observation": "UNKNOWN: no recorded tool result"})
    if len(steps) > max_steps:
        truncation.append({"kind": "oldest_steps", "count": len(steps) - max_steps})
        steps = steps[-max_steps:]
    for step in steps:
        if len(step["observation"]) > observation_chars:
            truncation.append({"kind": "observation", "step": step["step"], "original_chars": len(step["observation"])})
            step["observation"] = step["observation"][:observation_chars] + "\n[TRUNCATED; full output in experience]"
    evaluation = record["receipt"].get("evaluation") or {}
    # A benchmark may report reviewed and original outcomes together. Default to original.
    original = (evaluation.get("raw") or {}).get("original_evaluation")
    if original is not None:
        passed = original.get("pass", original.get("passed"))
        valid = evaluation.get("valid") is True and type(passed) is bool
        reward = float(passed) if valid else None
    else:
        valid, passed, reward = evaluation.get("valid") is True, evaluation.get("passed"), evaluation.get("reward")
    result = {"status": "measured" if valid else "unknown", "passed": passed if valid else None,
              "reward": reward if valid else None, "score_basis": "original", "verifier_version": evaluation.get("verifier_version")}
    return {"schema_version": "workagent-curator-input-v1", "experience_id": digest(record),
            "task_uid": record["task"]["uid"], "task_sha256": digest(record["task"]),
            "task_description": record["task"]["prompt"], "steps": steps, "result": result,
            "artifacts": deepcopy(record["artifacts"]), "costs": deepcopy(record["costs"]),
            "truncation": truncation, "training_admitted": False}
