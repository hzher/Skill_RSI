"""Framework-neutral Generator -> fixed Executor -> measured reward orchestration.

NeMo supplies the model/resources transports. This module does not implement an
environment loop or call an alternate local runner. An Executor is injected and
must consume the generated declarative skill; its private trace is not returned
as Generator training output.
"""
from __future__ import annotations

import json
import math
import hashlib
from copy import deepcopy
from typing import Protocol
import uuid

from training.skill_contract import (SCHEMA, TrainingRejected,
                                     canonical, pack_generator_trace, sign_capture, tokens, validate_skill)


class FixedExecutor(Protocol):
    executor_id: str

    async def execute(self, *, skill: dict, task: dict, resources) -> dict:
        """Return a measured outcome; save Executor trajectories separately."""
        ...


class InvalidSkillOutput(ValueError):
    def __init__(self, prompt, messages, reason):
        super().__init__(reason)
        self.prompt, self.messages, self.reason = prompt, messages, reason


def captured_generator_messages(response: dict, policy_id: str):
    """Read original token fields from the actual NeMo model response."""
    output = response.get("output", [])
    if len(output) != 1 or output[0].get("type") != "message":
        raise TrainingRejected("Generator must return exactly one declarative skill message")
    message = output[0]
    content = message.get("content", [])
    if not content or any(c.get("type") != "output_text" for c in content):
        raise TrainingRejected("Generator output is not a text skill")
    # No tokenizer call or fallback probabilities at this boundary.
    prompt = [{"role": "user", "actor": "context", "token_ids": message.get("prompt_token_ids")}]
    generated = {"role": "assistant", "actor": "generator", "policy_id": policy_id,
                 "token_ids": message.get("generation_token_ids"),
                 "generation_logprobs": message.get("generation_log_probs")}
    tokens(prompt[0])
    original = tokens(generated)
    lp = generated["generation_logprobs"]
    if not isinstance(lp, list) or len(lp) != len(original) or any(type(p) not in (int, float) or not math.isfinite(p) or p > 0 for p in lp):
        raise TrainingRejected("Actual Generator log probabilities are required before Executor use")
    try:
        skill = validate_skill(json.loads("".join(c["text"] for c in content)))
    except ValueError as exc:
        raise InvalidSkillOutput(prompt, prompt + [generated], type(exc).__name__) from exc
    return skill, prompt, prompt + [generated]


async def run_generated_skill(*, model_response: dict, downstream_task: dict, provenance: dict,
                              downstream_provenance: dict, identities: dict,
                              executor: FixedExecutor, resources, capture_key: bytes, global_step: int,
                              provider_request_id: str, training_run_id: str, episode_id: dict, capture_kind="provider_response"):
    # Freeze the sampled policy output and routing before downstream execution
    # yields control. Caller edits must not change this rollout's training data.
    model_response, downstream_task = deepcopy(model_response), deepcopy(downstream_task)
    provenance, downstream_provenance, identities = deepcopy((provenance, downstream_provenance, identities))
    episode_id = deepcopy(episode_id)
    if executor.executor_id != identities["fixed_executor_id"]:
        raise TrainingRejected("Executor changed during Generator rollout")
    if (not provenance.get("task_id") or not downstream_provenance.get("task_id")
            or downstream_provenance.get("source") != "toolathlon_gym" or downstream_provenance.get("split") != "train"
            or downstream_provenance.get("task_id") == provenance.get("task_id")
            or not isinstance(downstream_task.get("prompt"), str)
            or not downstream_task["prompt"].strip()):
        raise TrainingRejected("A distinct, nonempty downstream task is required")
    if not provider_request_id or type(global_step) is not int or global_step < 0 or not training_run_id:
        raise TrainingRejected("Missing actual model request or policy-step identity")
    invalid_reason = None
    try:
        skill, prompt, messages = captured_generator_messages(model_response, identities["generator_policy_id"])
    except InvalidSkillOutput as exc:
        skill, prompt, messages, invalid_reason = None, exc.prompt, exc.messages, exc.reason
    if invalid_reason is None:
        measurement = await executor.execute(skill=deepcopy(skill), task=deepcopy(downstream_task), resources=resources)
    else:
        score_invalid = getattr(executor, "invalid_skill_measurement", None)
        if score_invalid is None:
            raise TrainingRejected("Executor lacks the original-task invalid-skill verification path")
        measurement = await score_invalid(task=deepcopy(downstream_task), resources=resources)
        # A bad skill was never executed. A pre-completed seed is not a useful failure signal.
        if measurement.get("reward") == 1 and measurement.get("mask_sample") is False:
            measurement = {**measurement, "reward": None, "mask_sample": True, "evaluation_status": "unknown",
                           "failure_kind": "invalid_skill_but_reset_task_already_passed"}
    if measurement.get("mask_sample") is not False or measurement.get("reward") is None:
        measurement = {**measurement, "reward": None, "mask_sample": True, "evaluation_status": "unknown"}
    if executor.executor_id != identities["fixed_executor_id"]:
        raise TrainingRejected("Executor changed while executing the skill")
    skill_sha256 = hashlib.sha256(canonical(skill)).hexdigest() if skill is not None else None
    record = {"schema_version": SCHEMA, "training_target": "skill_generator", "provenance": provenance,
              "identities": identities, "measurement": measurement, "generated_skill": deepcopy(skill),
              "generated_skill_sha256": skill_sha256,
              "downstream_execution": {"provenance": downstream_provenance,
                                       "task_id": downstream_provenance["task_id"],
                                       "task_prompt_sha256": hashlib.sha256(downstream_task["prompt"].encode("utf-8")).hexdigest(),
                                       "skill_sha256": skill_sha256,
                                       "executor_id": executor.executor_id,
                                       "executor_trace_ref": measurement.get("executor_trace_ref"),
                                       "evaluation_ref": measurement.get("evaluation_ref")},
              "generation_status": "invalid_skill" if invalid_reason else "valid_skill", "invalid_skill_reason": invalid_reason,
              "capture": {"kind": capture_kind, "model_requests": 1, "provider_request_id": provider_request_id,
                          "capture_id": uuid.uuid4().hex, "global_step": global_step, "training_run_id": training_run_id,
                          "episode_id": episode_id},
              "input_message_log": prompt, "message_log": messages}
    if measurement.get("mask_sample") or measurement.get("reward") is None:
        return {"skill": skill, "measurement": measurement, "skill_capture": None,
                "training_eligible": False, "failure_kind": "resource_measurement_unknown"}
    pack_generator_trace(record, prompt_limit=65536, response_limit=16384)
    return {"skill": skill, "measurement": measurement,
            "skill_capture": {"record": record, "signature": sign_capture(record, capture_key)},
            "training_eligible": capture_kind == "provider_response"}
