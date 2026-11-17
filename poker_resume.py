"""Replay a verified Poker prefix and continue it with the official runner."""

from copy import deepcopy
from pathlib import Path

from src.interface import Query, Response, observation_marks_instance_complete, run_task
from src.tasks.exploitable_poker.task import Poker
from src.trace_storage import InteractionStep, TraceRecorder, serialize_instance_outcome


def _observation_payload(observation):
    return {
        "content": observation.content,
        "instance_complete": observation_marks_instance_complete(observation),
        "metadata": observation.metadata or {},
    }


def replay_poker_prefix(job: dict, prefix: dict) -> tuple[Poker, Query]:
    """Recover task state by applying saved actions without contacting a model."""
    params = job["task_params"]
    recorded_task = prefix["task"]
    recorded_params = recorded_task["params"]
    execution = prefix["execution"]
    schedule = params["schedule"]
    index = job["index"]
    expected = job["expected_instances"]
    interactions = prefix["interactions"]
    outcomes = prefix["instance_outcomes"]
    if (
        job["task"] != "exploitable_poker"
        or job["phase"] != "run"
        or prefix["phase"] != "run"
        or prefix["status"] != "running"
        or recorded_task["name"] != job["task"]
        or prefix["schedule"] != schedule
        or recorded_params["schedule"] != schedule
        or any(source.get("run_index") != index or source.get("rollout_index") != index
               for source in (params, recorded_params))
        or recorded_params["expected_num_instances"] != expected
        or execution["run_group_id"] != job["group"]
        or execution["run_index"] != index
        or execution["total_interactions"] != len(interactions)
        or prefix["result"] is not None
        or not interactions
        or not 0 < len(outcomes) < expected
    ):
        raise ValueError("Saved Poker prefix does not match the requested rollout")

    task = Poker(schedule=schedule)
    if task.num_instances != expected:
        raise ValueError("Official Poker schedule length differs from the saved rollout")
    task.prepare_run(index)
    query = task.reset()
    for step_number, interaction in enumerate(interactions, 1):
        feedback = query.feedback
        observed_query = {
            "prompt": query.prompt,
            "instance_id": query.instance_id,
            "instance_index": query.instance_index,
            "metadata": query.metadata or {},
            "response_schema": query.response_schema.__name__,
            "feedback": _observation_payload(feedback) if feedback is not None else None,
        }
        if interaction["step_number"] != step_number or interaction["query"] != observed_query:
            raise ValueError(f"Poker replay query differs at interaction {step_number}")
        saved = interaction["response"]
        action = query.response_schema.model_validate(saved["action"])
        if saved["action_type"] != "structured" or action.model_dump() != saved["action"]:
            raise ValueError(f"Poker replay action differs at interaction {step_number}")
        step = task.step(Response(action=action, metadata=deepcopy(saved["metadata"])))
        if (
            step.done != interaction["done"]
            or _observation_payload(step.observation) != interaction["observation"]
            or step.next_query is None
        ):
            raise ValueError(f"Poker replay feedback differs at interaction {step_number}")
        query = step.next_query

    actual = [serialize_instance_outcome(outcome) for outcome in task.get_instance_outcomes()]
    if len(actual) != len(outcomes) or any(
        result != {key: saved[key] for key in result}
        for result, saved in zip(actual, outcomes, strict=True)
    ):
        raise ValueError("Poker replay changed the saved instance outcomes")
    return task, query


class _ResumedTraceRecorder(TraceRecorder):
    def __init__(self, job, prefix, live_path, task, system):
        super().__init__(
            system_name="dsh",
            task_name=job["task"],
            system_params={
                **job["system_params"], "capture_identity": system.capture_identity,
            },
            task_params=deepcopy(prefix["task"]["params"]),
            run_group_id=job["group"],
            run_index=job["index"],
            task_brief=task.get_agent_brief(),
            phase="run",
            live_trace_path=live_path,
        )
        self.start_time = prefix["execution"]["start_time"]
        self._prefix_interactions = deepcopy(prefix["interactions"])
        self._prefix_outcomes = deepcopy(prefix["instance_outcomes"])
        self.interactions = [InteractionStep(**deepcopy(row)) for row in self._prefix_interactions]
        self.instance_outcomes = list(task.get_instance_outcomes())
        self._recording_interaction = False

    def record_interaction(self, step_number, *args, **kwargs):
        self._recording_interaction = True
        try:
            return super().record_interaction(
                step_number + len(self._prefix_interactions), *args, **kwargs,
            )
        finally:
            self._recording_interaction = False

    def write_live_snapshot(self, result=None, *, extra=None):
        # The official runner records the interaction before syncing its outcomes.
        if not self._recording_interaction:
            super().write_live_snapshot(result, extra=extra)

    def sync_instance_outcomes(self, outcomes):
        changed = super().sync_instance_outcomes(outcomes)
        if not changed:
            self.write_live_snapshot()
        return changed

    def _build_trace_payload(self, *, result, include_artifacts=True):
        trace = super()._build_trace_payload(
            result=result, include_artifacts=include_artifacts,
        )
        trace["interactions"][:len(self._prefix_interactions)] = deepcopy(self._prefix_interactions)
        trace["instance_outcomes"][:len(self._prefix_outcomes)] = deepcopy(self._prefix_outcomes)
        if trace["result"] is not None:
            trace["result"]["instance_outcomes"] = trace["instance_outcomes"]
        return trace


def resume_poker(job: dict, system, prefix: dict, live_path: Path, *, replayed=None) -> dict:
    """Run the remaining decisions using the already attached DSH session."""
    task, query = replayed if replayed is not None else replay_poker_prefix(job, prefix)
    recorder = _ResumedTraceRecorder(job, prefix, live_path, task, system)
    result = run_task(
        task, system, trace_recorder=recorder, show_progress=False,
        reset_system=False, initial_query=query, phase="run",
    )
    return recorder.finalize(result)
