"""Uniform benchmark task input; public imports remain stable after module split."""
from .contracts import (TASK_SCHEMA, SET_SCHEMA, digest, identifier, nonempty,
                        strict_json, validate_task, make_task, make_task_set,
                        validate_task_set, load_task_set)
from .registry import TaskPlugin, TaskPluginRegistry
from .json_source import JsonTaskPlugin
