"""Explicit executor plugin selection with whole-set validation before dispatch."""
from pathlib import Path

from integrations.benchmark_tasks import identifier, validate_task_set
from .contracts import ExecutorOptions


class ExecutorRegistry:
    def __init__(self):
        self._factories = {}

    def register(self, name, factory):
        identifier(name)
        if name in self._factories or not callable(factory):
            raise ValueError("Duplicate or invalid Executor plugin")
        self._factories[name] = factory

    def create(self, name, *, project_root):
        if name not in self._factories:
            raise ValueError("Unknown Executor plugin: " + name)
        return self._factories[name](project_root=Path(project_root).resolve())


class ExecutorService:
    def __init__(self, registry, *, project_root):
        self.registry, self.project_root = registry, Path(project_root).resolve()

    def run(self, task_set, *, executor="api", options, execute=False):
        if not isinstance(options, ExecutorOptions) or type(execute) is not bool:
            raise ValueError("Invalid Executor options")
        tasks = validate_task_set(task_set)
        backend = self.registry.create(executor, project_root=self.project_root)
        if not execute:
            return backend.plan(tasks, options)
        # Backends must preflight the complete set before starting any task.
        return backend.execute(tasks, options)


def default_executor_registry():
    from .api import APIExecutor
    registry = ExecutorRegistry()
    registry.register("api", APIExecutor)
    return registry
