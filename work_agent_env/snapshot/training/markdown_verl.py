"""Native pinned-verl integration. Import requires real Ray/torch/verl installations.

No remote commercial API trace is converted to training tokens. Native vLLM
sampling feeds actual token IDs/logprobs to verl; only the fixed Executor uses
the separate task API. Optimizer and weight synchronization remain inside verl.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import weakref

import numpy as np
from verl.checkpoint_engine import CheckpointEngineManager
from verl.experimental.agent_loop.agent_loop import AgentLoopBase, AgentLoopManager, AgentLoopOutput, AgentLoopMetrics
from verl.utils.ray_utils import auto_await

from evaluation.firewall import EvaluationFirewall
from integrations.benchmark_tasks.contracts import digest
from integrations.benchmark_tasks.store import save
from integrations.executors import ExecutorOptions
from integrations.experience import ExperienceStore
from integrations.skill_generator.editor import SkillEditor
from integrations.skill_library import SkillLibrary
from integrations.orchestration.executor import DownstreamExecutor
from integrations.orchestration.async_pipeline import finish_sample
from integrations.orchestration.journal import Journal
from training.algorithms import AlgorithmRegistry
from training.markdown_contract import native_prompt, prompt_messages, rollout_identity, validate_sample
from training.markdown_data import prepare_group, read_pairs
from training.rewards import seal_groups
from training.skill_contract import TrainingRejected, sign_capture
from training.updates import UpdateGate, checkpoint_identity

_runtimes = weakref.WeakKeyDictionary()


def runtime_config():
    path = Path(os.environ["WORKENV_MARKDOWN_RUNTIME_CONFIG"])
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != os.environ["WORKENV_MARKDOWN_RUNTIME_SHA256"]:
        raise TrainingRejected("Training runtime configuration changed")
    for variable, expected in (("WORKENV_TRAINING_REGISTRY", "WORKENV_TRAINING_REGISTRY_SHA256"),
                               ("WORKENV_MARKDOWN_PAIRS", "WORKENV_MARKDOWN_PAIRS_SHA256")):
        if hashlib.sha256(Path(os.environ[variable]).read_bytes()).hexdigest() != os.environ[expected]:
            raise TrainingRejected("Training data/admission registry changed during a run")
    manifest_path = Path(os.environ["WORKENV_MARKDOWN_SOURCE_MANIFEST"])
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != os.environ["WORKENV_MARKDOWN_SOURCE_MANIFEST_SHA256"]:
        raise TrainingRejected("Training source manifest changed")
    root = Path(os.environ["WORKENV_PROJECT_ROOT"])
    for name, expected in json.loads(manifest_bytes)["source_sha256"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise TrainingRejected("Training implementation changed during a policy batch")
    return json.loads(raw)


def capture_key():
    key = Path(os.environ["WORKENV_CAPTURE_KEY_FILE"]).read_bytes()
    if len(key) < 32:
        raise TrainingRejected("Invalid capture authentication key")
    return key


def gate():
    return UpdateGate(Path(os.environ["WORKENV_MARKDOWN_RUN_DIRECTORY"]),
                      run_id=os.environ["WORKENV_TRAINING_RUN_ID"], capture_key=capture_key())


def firewall():
    return EvaluationFirewall.load(Path(os.environ["WORKENV_VERIFIED_MANIFEST"]),
                                   Path(os.environ["WORKENV_TRAINING_REGISTRY"]))


def editor():
    root = Path(os.environ["WORKENV_PROJECT_ROOT"])
    return SkillEditor(ExperienceStore(root / ".runtime/experience"), SkillLibrary(root / ".runtime/skill_library"))


async def runtime():
    """One set of bounded stage permits per Ray worker/event loop."""
    loop = asyncio.get_running_loop()
    cached = _runtimes.setdefault(loop, {})
    if "task" not in cached:
        async def create():
            config = runtime_config()
            from scripts.run_skill_pipeline import load_credentials
            root = Path(os.environ["WORKENV_PROJECT_ROOT"])
            load_credentials(root / config["executor_credentials_file"])
            pairs = read_pairs(os.environ["WORKENV_MARKDOWN_PAIRS"])
            tasks = {pair["downstream_task"]["uid"]: pair["downstream_task"] for pair in pairs}
            fixed = await DownstreamExecutor.create(project_root=root,
                options=ExecutorOptions(**config["executor_options"]), tasks=list(tasks.values()))
            expected = os.environ["WORKENV_FIXED_EXECUTOR_ID"]
            if fixed.executor_id != expected:
                raise TrainingRejected("Deployment Executor differs from the frozen training plan")
            roles = None
            if config.get("skillos_roles_file"):
                from integrations.skill_generator.roles import SkillOSRoles, load_roles
                roles_config = load_roles(config["skillos_roles_file"])
                if digest(roles_config) != config["skillos_roles_sha256"]:
                    raise TrainingRejected("SkillOS role configuration changed")
                roles = SkillOSRoles(roles_config)
            return {"executor": fixed, "editor": editor(), "skillos_roles": roles,
                    "pipeline_slots": asyncio.Semaphore(config["queue_capacity_per_worker"]),
                    "generator_slots": asyncio.Semaphore(config["generator_concurrency_per_worker"]),
                    "executor_slots": asyncio.Semaphore(config["executor_concurrency_per_worker"])}
        cached["task"] = asyncio.create_task(create())
    return await cached["task"]


class MarkdownSkillAgentLoop(AgentLoopBase):
    async def run(self, sampling_params, **kwargs):
        shared = await runtime()
        async with shared["pipeline_slots"]:
            return await self._run_sample(sampling_params, shared=shared, **kwargs)

    async def _run_sample(self, sampling_params, *, shared, **kwargs):
        config = runtime_config()
        policy, request, pair = deepcopy((kwargs["skill_policy"], kwargs["skill_request"], kwargs["skill_pair"]))
        messages = prompt_messages(kwargs["raw_prompt"])
        if messages != native_prompt(request):
            raise TrainingRejected("Native Generator prompt changed after admission")
        if not sampling_params.get("logprobs"):
            raise TrainingRejected("Native sampling must return actual token logprobs")
        identity = kwargs["skill_rollout_id"]
        directory = Path(os.environ["WORKENV_MARKDOWN_RUN_DIRECTORY"])
        journal = Journal(directory)
        lease = journal.claim({"kind": "native_sample", "rollout_id": identity, "policy": policy})
        started = time.monotonic()
        try:
            async with shared["generator_slots"]:
                sample_started = time.monotonic()
                prompt_ids = await self.apply_chat_template(messages)
                if len(prompt_ids) > self.rollout_config.prompt_length:
                    raise TrainingRejected("Native Generator prompt exceeds configured limit")
                request_id = uuid.uuid4().hex
                journal.transition(lease, expected={"claimed"}, state="sampling_sent", payload={"sampling_request_id": request_id})
                async with asyncio.timeout(config["sample_timeout"]):
                    output = await self.server_manager.generate(request_id=request_id, prompt_ids=prompt_ids,
                                                               sampling_params=sampling_params)
                budget_terminated = len(output.token_ids) >= self.rollout_config.response_length
                retained_budget_failure = (shared["skillos_roles"] is not None and budget_terminated
                                           and output.stop_reason in ("completed", "length"))
                if ((output.stop_reason != "completed" or budget_terminated) and not retained_budget_failure):
                    raise TrainingRejected("Incomplete or token-budget-truncated Generator output")
                sample = {"schema_version": "workagent-generator-sample-v1", "kind": "verl_native",
                    "actor": "skill_generator", "request_id": request["id"], "provider_request_id": request_id,
                    "request_id_scope": "verl_server_manager_client_request",
                    "policy": policy, "prompt_ids": list(prompt_ids), "response_ids": list(output.token_ids),
                    "response_logprobs": output.log_probs,
                    "text": self.tokenizer.decode(output.token_ids, skip_special_tokens=True),
                    "decode_options": {"skip_special_tokens": True}, "finish_reason": "stop"}
                if budget_terminated:
                    sample.update(finish_reason="length", terminal_contract="skillos_budget_failure_retained")
                validate_sample(sample, policy=policy, request_id=request["id"])
                save(directory / "samples" / (identity + ".json"), sample)
                journal.transition(lease, expected={"sampling_sent"}, state="sampled")
                sampling_seconds = time.monotonic() - sample_started
            async with shared["executor_slots"]:
                task_started = time.monotonic()
                journal.transition(lease, expected={"sampled"}, state="executing")
                async with asyncio.timeout(config["executor_timeout"]):
                    row = await finish_sample(editor=shared["editor"], fixed_executor=shared["executor"],
                        request=request, task=pair["downstream_task"], sample=sample, policy=policy,
                        skillos_roles=shared["skillos_roles"], directory=directory / "judges" / identity)
                downstream_seconds = time.monotonic() - task_started
            row.update(rollout_id=identity, group_id=kwargs["skill_group_id"], slot=int(kwargs["skill_slot"]),
                request_id=request["id"], parent=request["parent"], downstream_sha256=digest(pair["downstream_task"]),
                seed_provenance=pair["seed_provenance"], downstream_provenance=pair["downstream_provenance"])
            row["signature"] = sign_capture(row, capture_key())
            save(directory / "rollouts" / (identity + ".json"), row)
            journal.transition(lease, expected={"executing"}, state="measured", payload={"rollout_id": identity})
            return AgentLoopOutput(prompt_ids=sample["prompt_ids"], response_ids=sample["response_ids"],
                response_mask=[1] * len(sample["response_ids"]), response_logprobs=sample["response_logprobs"],
                reward_score=row["reward"], num_turns=2,
                metrics=AgentLoopMetrics(generate_sequences=sampling_seconds, tool_calls=downstream_seconds),
                extra_fields={"skill_rollout": row, "turn_scores": [], "tool_rewards": [],
                    "skill_timings": {"sampling": sampling_seconds, "downstream": downstream_seconds,
                                      "total": time.monotonic() - started}})
        except BaseException as exc:
            journal.transition(lease, expected={"claimed", "sampling_sent", "sampled", "executing"}, state="unknown",
                               payload={"error": type(exc).__name__})
            raise


class MarkdownSkillAgentLoopManager(AgentLoopManager):
    @auto_await
    async def generate_sequences(self, prompts):
        if prompts.meta_info.get("validate", False):
            raise TrainingRejected("Independent evaluation must use its separate launcher")
        config = runtime_config()
        algorithm = AlgorithmRegistry().resolve(**config["algorithm"])
        if algorithm.max_policy_lag != 0:
            raise TrainingRejected("Native adapter currently requires on-policy update barriers")
        if self.rollout_config.n != algorithm.group_size or not self.rollout_config.calculate_log_probs:
            raise TrainingRejected("verl rollout cardinality/logprob settings differ from training plan")
        policy = gate().current_policy(prompts.meta_info["global_steps"],
                                       tokenizer_revision=os.environ["WORKENV_GENERATOR_TOKENIZER_REVISION"])
        batch_id = policy["run_id"] + ":step:" + str(policy["step"])
        edit, guard = editor(), firewall()
        requests, identities, slots, group_ids, expected = [], [], [], [], {}
        counts = {}
        for uid, pair, raw_prompt in zip(prompts.non_tensor_batch["uid"], prompts.non_tensor_batch["skill_pair"],
                                         prompts.non_tensor_batch["raw_prompt"], strict=True):
            uid = str(uid)
            group = prepare_group(pair, editor=edit, policy_id=policy["policy_id"], training=True, firewall=guard)
            if prompt_messages(raw_prompt) != native_prompt(group["request"]):
                raise TrainingRejected("Dataset input differs from reviewed rendered experience")
            slot = counts.get(uid, 0)
            counts[uid] = slot + 1
            identity = rollout_identity(batch_id=batch_id, group_id=uid, slot=slot, policy=policy,
                request_id=group["request"]["id"], downstream_sha256=digest(pair["downstream_task"]))
            expected.setdefault(uid, []).append(identity)
            requests.append(group["request"])
            identities.append(identity)
            slots.append(slot)
            group_ids.append(uid)
        if any(count != algorithm.group_size for count in counts.values()):
            raise TrainingRejected("Native batch contains incomplete groups")
        for key, values in {"skill_policy": [policy] * len(requests), "skill_request": requests,
                            "skill_rollout_id": identities, "skill_slot": slots, "skill_group_id": group_ids}.items():
            array = np.empty(len(values), dtype=object)
            array[:] = values
            prompts.non_tensor_batch[key] = array
        journal = Journal(Path(os.environ["WORKENV_MARKDOWN_RUN_DIRECTORY"]))
        lease = journal.claim({"kind": "native_rollout_batch", "batch_id": batch_id})
        journal.transition(lease, expected={"claimed"}, state="collecting")
        try:
            output = await super().generate_sequences(prompts)
            rows = list(output.non_tensor_batch["skill_rollout"])
            roles_config = None
            if config.get("skillos_roles_file"):
                from integrations.skill_generator.roles import load_roles
                roles_config = load_roles(config["skillos_roles_file"])
                if digest(roles_config) != config["skillos_roles_sha256"]:
                    raise TrainingRejected("SkillOS role configuration changed before batch sealing")
            sealed = seal_groups(rows, expected=expected, policy=policy, algorithm=algorithm, mode="training",
                                 firewall=guard, capture_key=capture_key(), roles_config=roles_config)
            gate().ready(sealed)
            journal.transition(lease, expected={"collecting"}, state="sealed", payload={"batch_id": sealed["id"]})
            output.meta_info["skill_batch_sha256"] = sealed["id"]
            return output
        except BaseException as exc:
            journal.transition(lease, expected={"collecting"}, state="unknown", payload={"error": type(exc).__name__})
            raise


class MarkdownCheckpointManager(CheckpointEngineManager):
    """Pinned trainer calls this after actor update/checkpoint, before next rollout."""
    @auto_await
    async def update_weights(self, global_steps=None):
        update_gate = gate()
        job, previous = update_gate.begin_install(global_steps)
        try:
            if global_steps == 0:
                initial_sha, _ = checkpoint_identity(os.environ["WORKENV_GENERATOR_MODEL_PATH"])
                if initial_sha != os.environ["WORKENV_GENERATOR_MODEL_REVISION"]:
                    raise TrainingRejected("Initial Generator checkpoint changed before weight installation")
            await super().update_weights(global_steps)
            if global_steps == 0:
                revision = os.environ["WORKENV_GENERATOR_MODEL_REVISION"]
                checkpoint = os.environ["WORKENV_GENERATOR_MODEL_PATH"]
            else:
                checkpoint = str(Path(os.environ["WORKENV_MARKDOWN_RUN_DIRECTORY"]) / "checkpoints" /
                                 ("global_step_" + str(global_steps)) / "actor")
                revision, files = checkpoint_identity(checkpoint)
                save(Path(os.environ["WORKENV_MARKDOWN_RUN_DIRECTORY"]) / "checkpoints" /
                     ("manifest-" + str(global_steps) + ".json"), {"sha256": revision, "files": files})
            update_gate.installed(job, step=global_steps, checkpoint_sha256=revision,
                checkpoint_ref=checkpoint, batch_id=previous["batch_id"])
        except BaseException as exc:
            update_gate.unknown(job, error=type(exc).__name__)
            raise
