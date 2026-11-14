"""Benchmark-neutral task sets and explicit executor dispatch. Standard library only.

Task conversion does not execute benchmark code, initialize environments, read
reference answers, grant training admission, or imply runtime readiness.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

TASK_SCHEMA = "workagent-task-v1"
SET_SCHEMA = "workagent-task-set-v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value) or value in (".", ".."):
        raise ValueError("Invalid benchmark/task/backend identifier")
    return value


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing " + label)
    return value


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("Nonfinite JSON value")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


def validate_task(task):
    fields = {"schema_version", "uid", "benchmark", "task_id", "source", "prompt",
              "system_prompt", "tools", "environment", "evaluation", "metadata"}
    if not isinstance(task, dict) or set(task) != fields or task["schema_version"] != TASK_SCHEMA:
        raise ValueError("Invalid task schema")
    bench, name = identifier(task["benchmark"]), identifier(task["task_id"])
    if task["uid"] != bench + ":" + name:
        raise ValueError("Task UID does not match namespace")
    nonempty(task["prompt"], "task prompt")
    if task["system_prompt"] is not None and not isinstance(task["system_prompt"], str):
        raise ValueError("Invalid system prompt")
    if not isinstance(task["source"], dict) or not isinstance(task["metadata"], dict):
        raise ValueError("Invalid task provenance/metadata")
    nonempty(task["source"].get("revision"), "source revision")
    nonempty(task["source"].get("plugin"), "source plugin")
    for key in ("environment", "evaluation"):
        binding = task[key]
        if not isinstance(binding, dict) or set(binding) != {"backend", "config"} or not isinstance(binding["config"], dict):
            raise ValueError("Invalid " + key + " binding")
        identifier(binding["backend"])
    if not isinstance(task["tools"], list):
        raise ValueError("Tools must be a list")
    names = set()
    for tool in task["tools"]:
        if not isinstance(tool, dict) or set(tool) != {"kind", "name"}:
            raise ValueError("Invalid tool requirement")
        kind, name = nonempty(tool["kind"], "tool kind"), nonempty(tool["name"], "tool name")
        if (kind, name) in names:
            raise ValueError("Duplicate tool requirement")
        names.add((kind, name))
    digest(task)  # Reject values that cannot cross the JSON boundary.
    return task


def make_task(*, benchmark, task_id, source, prompt, system_prompt=None,
              tools=None, environment, evaluation, metadata=None):
    task = dict(schema_version=TASK_SCHEMA, uid=benchmark + ":" + task_id,
                benchmark=benchmark, task_id=task_id, source=source, prompt=prompt,
                system_prompt=system_prompt, tools=tools or [], environment=environment,
                evaluation=evaluation, metadata=metadata or {})
    return deepcopy(validate_task(task))


def make_task_set(tasks):
    rows = [deepcopy(validate_task(task)) for task in tasks]
    if not rows or len({row["uid"] for row in rows}) != len(rows):
        raise ValueError("Empty task set or duplicate task UID")
    return dict(schema_version=SET_SCHEMA, tasks=rows, tasks_sha256=digest(rows))


def validate_task_set(value):
    if not isinstance(value, dict) or set(value) != {"schema_version", "tasks", "tasks_sha256"} or value["schema_version"] != SET_SCHEMA:
        raise ValueError("Invalid task-set schema")
    if not isinstance(value["tasks"], list):
        raise ValueError("Invalid tasks list")
    expected = make_task_set(value["tasks"])
    if value["tasks_sha256"] != expected["tasks_sha256"]:
        raise ValueError("Task-set checksum mismatch")
    return expected


def load_task_set(path):
    return validate_task_set(strict_json(Path(path).read_text(encoding="utf-8")))
