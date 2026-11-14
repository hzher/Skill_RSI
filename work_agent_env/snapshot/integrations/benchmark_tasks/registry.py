"""Task source plugin protocol, registration and deterministic selection."""
from typing import Protocol

from .contracts import identifier, make_task_set


class TaskPlugin(Protocol):
    def load(self) -> list[dict]: ...


class TaskPluginRegistry:
    def __init__(self):
        self._plugins = {}

    def register(self, name, factory):
        identifier(name)
        if name in self._plugins or not callable(factory):
            raise ValueError("Duplicate or invalid task plugin")
        self._plugins[name] = factory

    def convert(self, name, *, task_ids=None, **config):
        if name not in self._plugins:
            raise ValueError("Unsupported task plugin: " + name)
        result = make_task_set(self._plugins[name](**config).load())
        if task_ids is not None:
            selected = list(task_ids)
            if not selected or len(selected) != len(set(selected)):
                raise ValueError("Empty or duplicate task selection")
            rows = {row["task_id"]: row for row in result["tasks"]}
            if any(name not in rows for name in selected):
                raise ValueError("Unknown selected task")
            result = make_task_set(rows[name] for name in selected)
        return result
