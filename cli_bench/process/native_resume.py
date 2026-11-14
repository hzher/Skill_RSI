"""Reconstruct a stopped Cohort task from recorded actions and restore its native thread."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil


class NativeResume:
    def __init__(self, plan, archive):
        run_id = plan["resume_from_run"]
        if not re.fullmatch(r"collection_[A-Za-z0-9_+]+", run_id):
            raise ValueError("Invalid resume run identity")
        self.source = archive.parent / run_id
        self.archive = archive
        self.plan = plan
        previous = json.loads((self.source / "control/bridge-plan.json").read_text(encoding="utf-8-sig"))
        if plan["effective_config"]["task"]["name"] != "cohort_studies" or plan["include_baseline"]:
            raise ValueError("Verified action replay is restricted to one stateful Cohort run")
        for key in ["benchmark_sha", "source_key"]:
            if previous[key] != plan[key]:
                raise ValueError("Resume source differs: " + key)
        if previous["effective_config"]["task"] != plan["effective_config"]["task"]:
            raise ValueError("Resume task parameters differ")
        files = list((self.source / "native/results/cohort_studies/live").glob("*/run_*.json"))
        if not files:
            raise ValueError("No original partial task snapshot")
        self.trace_path = max(files, key=lambda p: p.stat().st_mtime)
        self.trace = json.loads(self.trace_path.read_text(encoding="utf-8-sig"))
        self.turns = self.trace["interactions"]
        if not self.turns:
            raise ValueError("No recorded actions to replay")
        self.position = 0
        self.pending = None
        self.restored = False
        self.outcome_indexes = {x["instance_index"] for x in self.trace.get("instance_outcomes", [])}
        self.session_id = self.turns[-1]["response"]["metadata"]["conversation_id"]
        self.evidence = {"source_run": run_id, "source_trace_sha256": hashlib.sha256(self.trace_path.read_bytes()).hexdigest(),
                         "recorded_interactions": len(self.turns), "previously_graded_instance_indexes": sorted(self.outcome_indexes),
                         "model_calls_for_reconstruction": 0, "source_archive_modified": False,
                         "status": "reconstructing_task"}
        self._save()

    def _save(self):
        (self.archive / "control/resume-evidence.json").write_text(json.dumps(self.evidence, indent=2), encoding="utf-8")

    def replay_response(self, query):
        if self.position == len(self.turns):
            if self.pending is not None:
                raise RuntimeError("Final replay observation has not been checked")
            return None
        if self.pending is not None:
            raise RuntimeError("Replay observation missing")
        turn = self.turns[self.position]
        expected = turn["query"]
        if any(getattr(query, name) != expected[name] for name in ["instance_id", "instance_index", "prompt"]):
            raise RuntimeError(f"Public query differs from original at replay interaction {self.position + 1}")
        from src.interface import Response
        action = query.response_schema.model_validate(turn["response"]["action"])
        self.pending = turn["observation"]
        self.position += 1
        return Response(action=action, metadata={"rsi_replayed": True, "source_run": self.plan["resume_from_run"],
                                                 "source_step": self.position, "model_calls": 0})

    def verify_observation(self, observation):
        if self.pending is None:
            return
        if observation.content != self.pending["content"] or observation.instance_complete != self.pending["instance_complete"]:
            raise RuntimeError(f"Environment observation differs at replay interaction {self.position}")
        self.pending = None
        if self.position == len(self.turns):
            self.evidence.update({"verified_public_queries": self.position, "verified_environment_observations": self.position,
                                  "status": "task_state_verified"})
            self._save()

    def restore_thread(self, system):
        if self.restored:
            return
        if self.position != len(self.turns) or self.pending is not None:
            raise RuntimeError("Task reconstruction is incomplete")
        suffix = "-" + self.session_id + ".jsonl"
        files = [p for p in (self.source / "native").rglob("*.jsonl")
                 if p.name.endswith(suffix) and "final" in p.parts and ".codex" in p.parts]
        if not files:
            raise RuntimeError("Native conversation archive is missing")
        source = max(files, key=lambda p: p.stat().st_size)
        metadata = json.loads(source.open(encoding="utf-8").readline())["payload"]
        if metadata["id"] != self.session_id or metadata.get("model_provider") != "rsi_router":
            raise RuntimeError("Wrong native thread or provider")
        relative = Path(*source.parts[source.parts.index(".codex"):])
        workspace = Path(system._tmp_dir).resolve()
        target = workspace / relative
        if not target.resolve().is_relative_to(workspace):
            raise RuntimeError("Unsafe native session destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        system._conversation_id = self.session_id
        system._conversation_session_ids = {self.session_id}
        system._interaction_count = len(self.turns)
        previous = dict(self.turns[-1]["response"]["metadata"]["cumulative_tokens"])
        # Include completed malformed/repair invocations already paid for.
        for path in sorted((self.source / "control/native-calls").glob("*.events.jsonl")):
            for raw in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if event.get("type") == "turn.completed" and event.get("usage"):
                    value = event["usage"]
                    previous = {"input_tokens": value.get("input_tokens", 0), "output_tokens": value.get("output_tokens", 0),
                                "reasoning_tokens": value.get("reasoning_tokens", value.get("reasoning_output_tokens", 0)),
                                "cached_input_tokens": value.get("cached_input_tokens", 0)}
        system._last_cumulative_token_usage_by_conversation[self.session_id] = previous
        self.restored = True
        self.evidence.update({"status": "native_thread_restored", "conversation_id": self.session_id,
                              "session_archive_path": source.relative_to(self.source).as_posix(),
                              "session_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                              "previous_thread_usage_not_rebilled": previous,
                              "native_session_copied_without_auth_files": True})
        self._save()
