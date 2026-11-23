"""JSONL bridge executed by the official CL-Bench environment."""
from contextlib import redirect_stdout
import inspect
import json
import sys
import traceback


def query_view(query):
    if query is None or (query.metadata or {}).get("done"):
        return None
    return {"query": query.prompt, "instance_id": query.instance_id, "instance_index": query.instance_index,
            "response_schema": query.response_schema.model_json_schema()}


class Session:
    def __init__(self):
        self.task = self.query = None
        self.complete = False
        self.outcomes = {}
        self.selected_indices = None

    def _collect(self, outcomes):
        for row in outcomes:
            self.outcomes[row.instance_id, row.instance_index] = row

    def reset(self, family, parameters, instance_indices, seed):
        from src.registry import get_task_class

        if self.task is not None:
            raise ValueError("one sequence per worker")
        cls = get_task_class(family)
        self.selected_indices = instance_indices
        if "seed" not in inspect.signature(cls).parameters:
            raise ValueError("official task does not expose a seed parameter")
        self.task = cls(**(parameters | {"seed": seed}))
        if instance_indices is None:
            self.query = self.task.reset()
        else:
            if not isinstance(instance_indices, list) or not instance_indices or len(set(instance_indices)) != len(instance_indices):
                raise ValueError("CL-Bench instance indices must be a nonempty unique sequence")
            self.task.build_canonical_run_state()
            self.task.select_run_instances(instance_indices)
            self.query = self.task.build_current_query()
        view = query_view(self.query)
        self.complete = view is None
        return {"current": view, "done": self.complete}

    def step(self, action):
        from src.interface import Response

        if self.query is None or self.complete:
            raise ValueError("sequence does not accept more actions")
        native = self.query.response_schema.model_validate(action)
        result = self.task.step(Response(action=native))
        self.query = result.next_query
        self.complete = bool(result.done)
        if result.instance_outcome is not None:
            self._collect((result.instance_outcome,))
        self._collect(self.task.get_instance_outcomes())
        return {"action": native.model_dump(mode="json"),
                "observation": {"content": result.observation.content,
                                "instance_complete": result.observation.instance_complete},
                "current": None if self.complete else query_view(result.next_query), "done": self.complete}

    def score(self):
        if not self.complete:
            raise ValueError("cannot score an unfinished sequence")
        result = self.task.evaluate()
        self._collect(result.instance_outcomes or self.task.get_instance_outcomes())
        rows = list(self.outcomes.values())
        if self.selected_indices is not None and [row.instance_index for row in rows] != self.selected_indices:
            raise ValueError("official score sequence does not cover the selected instance order")
        entries = ("instance_id", "instance_index", "reward", "success", "raw_metric_name",
                   "raw_metric_value", "raw_metric_higher_is_better")
        return {"instance_outcomes": [{key: getattr(row, key) for key in entries}
                                      for row in rows]}

    def close(self):
        if self.task is None:
            return
        cleanup = getattr(self.task, "cleanup", None)
        if callable(cleanup):
            cleanup()
            return
        cleanup_container = getattr(self.task, "_cleanup_container", None)
        if callable(cleanup_container):
            cleanup_container()
        for name in ("_conn", "_executor"):
            resource = getattr(self.task, name, None)
            if callable(getattr(resource, "close", None)):
                resource.close()


def main():
    session, stdout = Session(), sys.stdout
    methods = {"reset": session.reset, "step": session.step, "score": session.score}
    with redirect_stdout(sys.stderr):
        try:
            for raw in sys.stdin:
                try:
                    call = json.loads(raw)
                    result = methods[call["method"]](**call["params"])
                    payload = json.dumps({"result": result}, ensure_ascii=False, allow_nan=False)
                except Exception as exc:
                    traceback.print_exc(file=sys.stderr)
                    payload = json.dumps({"error": type(exc).__name__})
                print(payload, file=stdout, flush=True)
        finally:
            session.close()


if __name__ == "__main__":
    main()
