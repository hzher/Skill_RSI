"""Bounded asynchronous Generator -> isolated skill -> fixed Executor -> reward batches."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from integrations.benchmark_tasks.contracts import digest
from integrations.benchmark_tasks.store import save
from training.markdown_contract import rollout_identity, validate_policy, validate_sample
from training.rewards import bind_reward, seal_groups
from training.skill_contract import TrainingRejected, sign_capture
from .journal import Journal


@dataclass(frozen=True)
class PipelineLimits:
    generator_concurrency: int = 4
    executor_concurrency: int = 4
    queue_capacity: int = 8
    sample_timeout: float = 600
    executor_timeout: float = 2400

    def __post_init__(self):
        import math
        for value in (self.generator_concurrency, self.executor_concurrency, self.queue_capacity):
            if type(value) is not int or value < 1:
                raise ValueError("Concurrency and queue capacity must be positive")
        for value in (self.sample_timeout, self.executor_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("Invalid stage timeout")


async def finish_sample(*, editor, fixed_executor, request, task, sample, policy, skillos_roles=None, directory=None):
    """Shared by the standalone scheduler and native verl agent loop."""
    sample = validate_sample(sample, policy=policy, request_id=request["id"])
    executor_id = fixed_executor.executor_id
    candidate = await asyncio.to_thread(editor.apply, request, sample["text"], sampling={"native_sample": sample},
                                      **({"record_failures": True} if skillos_roles is not None else {}))
    execution = await fixed_executor.execute(candidate=candidate, task=task)
    if fixed_executor.executor_id != executor_id or digest(execution["task"]) != digest(task):
        raise TrainingRejected("Downstream task or frozen Executor changed during execution")
    result = bind_reward(candidate=candidate, execution=execution, sample=sample, policy=policy,
        request_id=request["id"], executor_id=executor_id)
    if skillos_roles is not None:
        from integrations.experience import curator_view
        assessed = await skillos_roles.assess(candidate=candidate, downstream_view=curator_view(execution),
            prompt_ids=sample["prompt_ids"], directory=directory)
        result.update(skillos_reward=assessed, reward=assessed["reward"])
    return result


class AsyncPipeline:
    def __init__(self, *, directory, editor, sampler, fixed_executor, algorithm, limits=None,
                 mode="diagnostic", firewall=None, capture_key=None):
        if mode not in ("diagnostic", "training"):
            raise ValueError("Unknown pipeline mode")
        self.directory = Path(directory)
        self.journal = Journal(self.directory)
        self.editor, self.sampler, self.fixed_executor = editor, sampler, fixed_executor
        self.algorithm, self.limits = algorithm, limits or PipelineLimits()
        self.mode, self.firewall, self.capture_key = mode, firewall, capture_key

    async def collect(self, *, batch_id, policy, groups):
        """Groups share an exact request, parent and downstream task across all K slots.

        Pipeline stages overlap within one policy snapshot. Queue bounds apply to
        completed sampling outputs waiting for task environments. No update can
        begin until the complete batch has been sealed.
        """
        policy, groups = deepcopy(validate_policy(policy)), deepcopy(groups)
        if not groups or len({group["group_id"] for group in groups}) != len(groups):
            raise TrainingRejected("Empty batch or duplicate group identity")
        if self.mode == "training" and (self.firewall is None or self.capture_key is None):
            raise TrainingRejected("Training requires reviewed provenance and an authentication key")
        if self.mode == "training":
            from training.markdown_data import prepare_group
            for group in groups:
                checked = prepare_group(group["pair"], editor=self.editor, policy_id=policy["policy_id"],
                                        training=True, firewall=self.firewall)
                if checked != group:
                    raise TrainingRejected("Training group differs from the reviewed pair")
        lease = self.journal.claim({"kind": "rollout_batch", "batch_id": batch_id, "policy": policy})
        self.journal.transition(lease, expected={"claimed"}, state="collecting")
        generated = asyncio.Queue(maxsize=self.limits.queue_capacity)
        pending = asyncio.Queue(maxsize=self.limits.queue_capacity)
        rows, expected, failures = [], {}, []
        async def feed():
            for group in groups:
                request, task = group["request"], group["downstream_task"]
                if request["generator_policy_id"] != policy["policy_id"]:
                    raise TrainingRejected("Editor request is from a different policy snapshot")
                expected[group["group_id"]] = []
                for slot in range(self.algorithm.group_size):
                    identity = rollout_identity(batch_id=batch_id, group_id=group["group_id"], slot=slot,
                        policy=policy, request_id=request["id"], downstream_sha256=digest(task))
                    expected[group["group_id"]].append(identity)
                    await pending.put((group, slot, identity))
            for _ in range(self.limits.generator_concurrency):
                await pending.put(None)
        async def generate():
            while True:
                item = await pending.get()
                if item is None:
                    return
                group, slot, identity = item
                job = self.journal.claim({"rollout_id": identity, "batch_id": batch_id, "policy": policy})
                try:
                    self.journal.transition(job, expected={"claimed"}, state="sampling_sent")
                    async with asyncio.timeout(self.limits.sample_timeout):
                        sample = await self.sampler.sample(request=group["request"], policy=policy,
                                                          rollout_id=identity, slot=slot)
                    sample = validate_sample(sample, policy=policy, request_id=group["request"]["id"])
                    if self.mode == "training" and sample["kind"] != "verl_native":
                        raise TrainingRejected("Diagnostic/API samples cannot enter training")
                    save(self.directory / "samples" / (identity + ".json"), sample)
                    self.journal.transition(job, expected={"sampling_sent"}, state="sampled")
                    await generated.put((group, slot, identity, job, sample))
                except BaseException as exc:
                    self.journal.transition(job, expected={"sampling_sent", "sampled"}, state="unknown",
                        payload={"error": type(exc).__name__})
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    failures.append({"rollout_id": identity, "stage": "sampling", "error": type(exc).__name__})
        async def execute():
            while True:
                item = await generated.get()
                if item is None:
                    return
                group, slot, identity, job, sample = item
                try:
                    self.journal.transition(job, expected={"sampled"}, state="executing")
                    async with asyncio.timeout(self.limits.executor_timeout):
                        row = await finish_sample(editor=self.editor, fixed_executor=self.fixed_executor,
                            request=group["request"], task=group["downstream_task"], sample=sample, policy=policy)
                    row.update(rollout_id=identity, group_id=group["group_id"], slot=slot,
                        request_id=group["request"]["id"], parent=group["request"]["parent"],
                        downstream_sha256=digest(group["downstream_task"]),
                        seed_provenance=group.get("seed_provenance"), downstream_provenance=group.get("downstream_provenance"))
                    if self.mode == "training":
                        row["signature"] = sign_capture(row, self.capture_key)
                    save(self.directory / "rollouts" / (identity + ".json"), row)
                    rows.append(row)
                    self.journal.transition(job, expected={"executing"}, state="measured", payload={"rollout_ref": identity})
                except BaseException as exc:
                    self.journal.transition(job, expected={"executing"}, state="unknown", payload={"error": type(exc).__name__})
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    failures.append({"rollout_id": identity, "stage": "execution", "error": type(exc).__name__})
        workers = [asyncio.create_task(execute()) for _ in range(self.limits.executor_concurrency)]
        producers = [asyncio.create_task(generate()) for _ in range(self.limits.generator_concurrency)]
        feeder = asyncio.create_task(feed())
        async def produce_all():
            await asyncio.gather(feeder, *producers)
            for _ in workers:
                await generated.put(None)
        supervisor = asyncio.create_task(produce_all())
        try:
            await asyncio.gather(supervisor, *workers)
            if failures:
                raise TrainingRejected("Batch has unknown/invalid rollouts; inspect failures.json")
            sealed = seal_groups(rows, expected=expected, policy=policy, algorithm=self.algorithm, mode=self.mode,
                                 firewall=self.firewall, capture_key=self.capture_key)
            save(self.directory / "batches" / (sealed["id"] + ".json"), sealed)
            self.journal.transition(lease, expected={"collecting"}, state="sealed", payload={"batch_sha256": sealed["id"]})
            return sealed
        except BaseException:
            for task in [supervisor, feeder, *producers, *workers]:
                task.cancel()
            await asyncio.gather(supervisor, feeder, *producers, *workers, return_exceptions=True)
            save(self.directory / "failures.json", failures)
            self.journal.transition(lease, expected={"collecting"}, state="unknown", payload={"failures": len(failures)})
            raise
