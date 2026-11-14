"""Fixed API Executor adapter for independently initialized downstream tasks."""
from dataclasses import replace
from pathlib import Path
import uuid

from integrations.benchmark_tasks import make_task_set
from integrations.executors.api import APIExecutor
from integrations.experience import ExperienceStore
from training.skill_contract import TrainingRejected


class DownstreamExecutor:
    def __init__(self, *, project_root, options, executor_id, backend=None):
        if options.skill_version is not None:
            raise ValueError("The rollout candidate, not a constructor, selects the skill version")
        self.root, self.options = Path(project_root), options
        self.executor_id = executor_id
        self.backend = backend or APIExecutor(project_root=self.root)
        self.store = ExperienceStore(self.root / ".runtime/experience")

    @classmethod
    async def create(cls, *, project_root, options, tasks, backend=None):
        backend = backend or APIExecutor(project_root=project_root)
        identities = []
        for task in tasks:
            plan = await backend.freeze_plan(make_task_set([task]), options)
            identities.append(plan["fixed_executor_id"])
        if not identities or len(set(identities)) != 1:
            raise TrainingRejected("One training batch requires one frozen Executor/environment configuration")
        return cls(project_root=project_root, options=options, executor_id=identities[0], backend=backend)

    async def execute(self, *, candidate, task):
        options = replace(self.options, run_id="skill_rollout_" + uuid.uuid4().hex,
                          skill_version=candidate["version"])
        task_set = make_task_set([task])
        plan = await self.backend.freeze_plan(task_set, options)
        if plan["fixed_executor_id"] != self.executor_id:
            raise TrainingRejected("Fixed Executor changed before downstream execution")
        receipt = await self.backend.execute_async(task_set, options)
        row = receipt["results"][0]
        if row.get("resources_closed") is not True or "experience" not in row:
            raise TrainingRejected("Downstream resources or evidence are unknown")
        execution = self.store.get(row["experience"]["id"])
        if execution["executor"]["fixed_executor_id"] != self.executor_id:
            raise TrainingRejected("Fixed Executor identity changed during execution")
        return execution
