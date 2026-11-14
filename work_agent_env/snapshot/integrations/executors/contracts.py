"""Executor options and backend protocol; input remains the task plugin's TaskSet."""
from dataclasses import dataclass, field
from typing import Protocol
import uuid
import re

from integrations.benchmark_tasks import identifier


@dataclass(frozen=True)
class ExecutorOptions:
    provider: str
    concurrency: int = 1
    run_id: str = field(default_factory=lambda: "executor_" + uuid.uuid4().hex)
    tool_scope: str = "all"
    environment_image: str | None = None
    model: str | None = None
    max_turns: int = 50
    max_output_tokens: int = 4096
    task_timeout: float = 1800
    request_timeout: float = 180
    skill_version: str | None = None

    def __post_init__(self):
        if self.provider not in ("openai", "anthropic"):
            raise ValueError("Unsupported API provider")
        if self.tool_scope not in ("all", "task"):
            raise ValueError("Invalid tool scope")
        if self.environment_image is not None and (not isinstance(self.environment_image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,255}", self.environment_image)):
            raise ValueError("Invalid environment image")
        if type(self.concurrency) is not int or self.concurrency < 1:
            raise ValueError("Concurrency must be a positive integer")
        identifier(self.run_id)
        if self.skill_version is not None and not re.fullmatch(r"[0-9a-f]{64}", self.skill_version):
            raise ValueError("Skill version must be a snapshot SHA256")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.run_id):
            raise ValueError("Invalid run ID")
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("Invalid model name")
        for value in (self.max_turns, self.max_output_tokens):
            if type(value) is not int or value < 1:
                raise ValueError("Invalid execution budget")
        import math
        for value in (self.task_timeout, self.request_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("Invalid timeout")


class ExecutorBackend(Protocol):
    def plan(self, task_set: dict, options: ExecutorOptions) -> dict: ...
    def execute(self, task_set: dict, options: ExecutorOptions) -> dict: ...
