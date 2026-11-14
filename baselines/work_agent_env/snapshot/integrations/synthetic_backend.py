"""Isolated synthetic resource operations; NeMo owns the interaction lifecycle.

No agent loop or reference solver is invoked here. Restarts invalidate orphan
sessions and retain their workspaces; they do not resume uncertain operations.
"""
from copy import deepcopy
import hashlib
import json
import threading
import uuid

from integrations.resource_ledger import ResourceLedger
from workenv.core import ROOT, load_task, owned_run, run_directory, save
from workenv.sessions import FileSession


class SyntheticConflict(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("A canonical resource/request UUID is required")
    return value


class SyntheticResources:
    def __init__(self, ledger=None):
        self.ledger = ledger or ResourceLedger(ROOT / ".runtime/nemo-vm/synthetic-sessions.json", ROOT)
        self.sessions = {}
        self.lock = threading.RLock()

    def recover(self):
        # Called while the bridge holds its exclusive owned-process lock.
        with self.lock:
            for sid, row in self.ledger.rows.items():
                if row["state"] != "closed":
                    if row["run_ref"]:
                        run = ROOT / row["run_ref"]
                        if run_directory(run.name) != run.resolve():
                            raise ValueError("Synthetic run escaped its owned directory")
                        if run.exists() and (run / "run.json").exists():
                            owned_run(run)
                            save(run / "nemo_synthetic_orphaned.json", {
                                "resources_session_id": sid, "reward": None, "mask_sample": True,
                                "training_eligible": False, "reason": "resource_process_restarted"})
                    self.ledger.update(sid, state="closed")

    def status(self):
        with self.lock:
            return {"source": "project_synthetic", "training_eligible": False,
                    "active_sessions": [sid for sid, row in self.ledger.rows.items() if row["state"] != "closed"],
                    "retained_sessions": len(self.ledger.rows)}

    def reap(self):
        with self.lock:
            for sid in self.ledger.expired():
                self._close(sid, self.ledger.rows[sid]["binding"]["episode_id"], "resource_lease_expired")

    def close_all(self):
        with self.lock:
            for sid, row in self.ledger.rows.items():
                if row["state"] != "closed":
                    self._close(sid, row["binding"]["episode_id"], "resource_server_shutdown")

    def _close(self, sid, episode, reason="nemo_resource_closed"):
        row = self.ledger.rows.get(sid) or self.ledger.retired.get(sid)
        if row is not None and row["binding"]["episode_id"] != episode:
            raise SyntheticConflict("Close episode mismatch")
        if row is None:
            self.ledger.close_unknown(sid, {"episode_id": deepcopy(episode)})
        elif row["state"] != "closed":
            self.ledger.update(sid, state="closing")
            state = self.sessions.get(sid)
            if state:
                state["session"].close(reason)
            self.ledger.update(sid, state="closed")
        self.sessions.pop(sid, None)
        return {"resources_session_id": sid}

    def dispatch(self, route, body):
        with self.lock:
            self.reap()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            sid = identifier(body.get("resources_session_id"))
            episode = body.get("episode_id")
            if not isinstance(episode, dict) or not isinstance(episode.get("rollout_id"), str):
                raise ValueError("An episode identity is required")
            if route == "/seed_session":
                if set(body) != {"resources_session_id", "episode_id", "task_id", "task_data"}:
                    raise ValueError("Unexpected seed fields")
                task = body["task_id"]
                if not isinstance(task, dict) or task.get("taskset") != "project_synthetic:diagnostic" or body["task_data"]:
                    raise ValueError("Only project_synthetic diagnostic resources are accepted")
                spec = load_task(task.get("task_id"))
                if spec.get("source") != "project_synthetic" or spec.get("runtime_adapter") not in ("local_files", "local_tickets"):
                    raise ValueError("Task is outside the synthetic diagnostic source")
                if sid in self.ledger.retired:
                    raise SyntheticConflict("Retired session cannot be recreated")
                if sid in self.ledger.rows:
                    row = self.ledger.rows[sid]
                    if row["state"] != "active" or row["binding"] != body or sid not in self.sessions:
                        raise SyntheticConflict("Session cannot be recreated or rebound")
                    return {"resources_session_id": sid}
                if len(self.sessions) >= 4:
                    raise SyntheticConflict("Four isolated synthetic resources are already active")
                self.ledger.seed(sid, body)
                run_id = "nemo_synthetic_" + uuid.uuid4().hex
                self.ledger.update(sid, run_ref="runs/" + run_id)
                try:
                    session = FileSession(spec["task_id"], run_id=run_id, max_tool_calls=100)
                    self.sessions[sid] = {"session": session, "requests": {}, "verify_count": 0}
                    self.ledger.update(sid, state="active")
                except Exception:
                    if sid in self.sessions:
                        self.sessions[sid]["session"].close("resource_error")
                        del self.sessions[sid]
                    self.ledger.update(sid, state="closed")
                    raise
                return {"resources_session_id": sid}
            if route == "/close_session":
                return self._close(sid, episode)
            row, state = self.ledger.rows.get(sid), self.sessions.get(sid)
            if row is None or row["state"] != "active" or state is None or state["session"].finished:
                raise SyntheticConflict("Resource session is missing or closed")
            if row["binding"]["episode_id"] != episode:
                raise SyntheticConflict("Episode identity does not match the resource session")
            self.ledger.update(sid, state="active")
            session = state["session"]
            if route == "/tools":
                return {**session.opened(), "source": "project_synthetic", "training_eligible": False}
            if route == "/verify":
                state["verify_count"] += 1
                if state["verify_count"] > 10:
                    raise SyntheticConflict("Synthetic verification budget exhausted")
                result = session.adapter.evaluate(session.run, "external_tools")
                target = session.run / ("nemo_verify_" + uuid.uuid4().hex + ".json")
                save(target, result)
                valid = result.get("evaluation_status") == "completed" and type(result.get("task_passed")) is bool
                return {"reward": float(result["task_passed"]) if valid else None, "mask_sample": not valid,
                        "source": "project_synthetic", "training_eligible": False, "evaluation": result,
                        "reward_version": "project-synthetic-diagnostic-v1",
                        "run_ref": session.run.relative_to(ROOT).as_posix(), "evaluation_ref": target.relative_to(ROOT).as_posix()}
            if route != "/tool":
                raise ValueError("Unknown synthetic resource route")
            rid = identifier(body.get("request_id"))
            fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()
            if rid in state["requests"]:
                previous = state["requests"][rid]
                if previous["fingerprint"] != fingerprint or "result" not in previous:
                    raise SyntheticConflict("Request reused or outcome unknown; do not replay")
                return deepcopy(previous["result"])
            if len(state["requests"]) >= 100:
                raise SyntheticConflict("Synthetic tool budget exhausted")
            previous = state["requests"][rid] = {"fingerprint": fingerprint}
            result = {"result": session.call(body.get("name"), body.get("arguments"))}
            previous["result"] = deepcopy(result)
            return result
