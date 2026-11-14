"""Convert public JSON benchmark exports to the common TaskSpec."""
import hashlib
from pathlib import Path

from .contracts import make_task, strict_json, validate_task_set


class JsonTaskPlugin:
    """Adapter for other benchmarks exported as public task JSON.

    Input: {benchmark, revision, environment, evaluation, tasks: [{task_id,
    prompt, system_prompt?, tools?, metadata?}]}. No command is executed.
    """
    def __init__(self, source):
        self.source = Path(source)

    def load(self):
        raw = self.source.read_bytes()
        data = strict_json(raw.decode("utf-8"))
        if isinstance(data, dict) and data.get("schema_version") == "workagent-task-set-v1":
            return validate_task_set(data)["tasks"]
        if set(data) != {"benchmark", "revision", "environment", "evaluation", "tasks"}:
            raise ValueError("Invalid JSON benchmark manifest")
        if not isinstance(data["tasks"], list):
            raise ValueError("Invalid benchmark tasks")
        rows = []
        for task in data["tasks"]:
            if not isinstance(task, dict) or not {"task_id", "prompt"} <= set(task) or set(task) - {"task_id", "prompt", "system_prompt", "tools", "metadata", "environment", "evaluation"}:
                raise ValueError("Invalid JSON benchmark task")
            rows.append(make_task(benchmark=data["benchmark"], task_id=task["task_id"],
                source={"plugin": "json", "revision": data["revision"],
                        "manifest_sha256": hashlib.sha256(raw).hexdigest()},
                prompt=task["prompt"], system_prompt=task.get("system_prompt"),
                tools=task.get("tools", []), metadata=task.get("metadata", {}),
                environment=task.get("environment", data["environment"]),
                evaluation=task.get("evaluation", data["evaluation"])))
        return rows
