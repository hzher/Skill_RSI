"""Strict Generator-only capture admission before the upstream verl recipe.

No tokenization, invented log probabilities, dummy samples, or Executor loss.
Authentication keys belong to the collection process, never to exported data.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import math
from typing import Any

SCHEMA = "workagent-skill-generator-capture-v2"
REWARD_VERSION = "toolathlon-original-and-supplemental-v1"


class TrainingRejected(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sign_capture(record: dict, key: bytes) -> str:
    if len(key) < 32:
        raise TrainingRejected("Capture authentication key is too short")
    return hmac.new(key, canonical(record), hashlib.sha256).hexdigest()


def validate_skill(value: dict) -> dict:
    required = {"schema_version", "skill_id", "objective", "steps", "allowed_tools"}
    if not isinstance(value, dict) or set(value) != required or value["schema_version"] != "skill-v1":
        raise TrainingRejected("Unsupported skill schema")
    for name, limit in (("skill_id", 100), ("objective", 2000)):
        if not isinstance(value[name], str) or not 0 < len(value[name].strip()) <= limit:
            raise TrainingRejected("Invalid skill " + name)
    if not isinstance(value["steps"], list) or not 1 <= len(value["steps"]) <= 32:
        raise TrainingRejected("Skill needs 1..32 declarative steps")
    if any(not isinstance(s, str) or not 0 < len(s.strip()) <= 2000 for s in value["steps"]):
        raise TrainingRejected("Invalid declarative skill step")
    tools = value["allowed_tools"]
    if not isinstance(tools, list) or len(tools) > 32 or any(not isinstance(t, str) or not 0 < len(t) <= 100 for t in tools):
        raise TrainingRejected("Invalid skill tool names")
    if len(set(tools)) != len(tools):
        raise TrainingRejected("Duplicate skill tool names")
    return value


@dataclass(frozen=True)
class GeneratorTokens:
    prompt_ids: list[int]
    response_ids: list[int]
    response_mask: list[int]
    response_logprobs: list[float]
    reward: float
    generator_policy_id: str
    fixed_executor_id: str
    capture_sha256: str


def tokens(message):
    ids = message.get("token_ids")
    if not isinstance(ids, list) or not ids or any(type(t) is not int or t < 0 for t in ids):
        raise TrainingRejected("Original nonempty token IDs are required")
    return ids


def pack_generator_trace(record: dict, *, prompt_limit: int, response_limit: int) -> GeneratorTokens:
    """Validate a trace. Pure conversion alone does not admit it to training."""
    if record.get("schema_version") != SCHEMA:
        raise TrainingRejected("Unsupported capture schema")
    if record.get("training_target") != "skill_generator":
        raise TrainingRejected("Training target must be Skill Generator")
    ids = record.get("identities", {})
    for name in ("generator_policy_id", "generator_model_revision", "tokenizer_revision", "fixed_executor_id", "environment_version"):
        if not isinstance(ids.get(name), str) or not ids[name].strip():
            raise TrainingRejected("Missing frozen identity: " + name)
    provenance = record.get("provenance", {})
    if provenance.get("source") != "toolathlon_gym" or provenance.get("split") != "train":
        raise TrainingRejected("Only designated Toolathlon-GYM training seeds are eligible")
    for field in ("task_id", "task_family", "source_commit", "seed_sha256", "prompt_sha256"):
        if not isinstance(provenance.get(field), str) or not provenance[field]:
            raise TrainingRejected("Missing training provenance: " + field)
    execution = record.get("downstream_execution", {})
    downstream = execution.get("provenance", {})
    if downstream.get("source") != "toolathlon_gym" or downstream.get("split") != "train":
        raise TrainingRejected("Downstream task must be a reviewed training task")
    if not downstream.get("task_id") or downstream["task_id"] == provenance["task_id"]:
        raise TrainingRejected("Frozen Skill must be evaluated on a distinct downstream task")
    if execution.get("task_id") != downstream["task_id"] or execution.get("executor_id") != ids["fixed_executor_id"]:
        raise TrainingRejected("Downstream task or frozen Executor identity changed")
    task_prompt_sha256 = execution.get("task_prompt_sha256")
    if (not isinstance(task_prompt_sha256, str) or len(task_prompt_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in task_prompt_sha256)):
        raise TrainingRejected("Downstream task prompt identity is missing")
    if execution.get("skill_sha256") != record.get("generated_skill_sha256"):
        raise TrainingRejected("Executed Skill differs from Generator output")
    skill = record.get("generated_skill")
    if skill is not None:
        if record.get("generation_status") != "valid_skill":
            raise TrainingRejected("Executable Skill must have valid generation status")
        validate_skill(skill)
        if hashlib.sha256(canonical(skill)).hexdigest() != execution["skill_sha256"]:
            raise TrainingRejected("Frozen Skill bytes differ from Generator output")
    elif record.get("generation_status") != "invalid_skill" or execution.get("skill_sha256") is not None:
        raise TrainingRejected("Missing generated Skill without invalid-skill status")
    measured = record.get("measurement", {})
    if measured.get("mask_sample") is not False or measured.get("evaluation_status") != "completed":
        raise TrainingRejected("Unknown or masked resource measurements are excluded")
    reward = measured.get("reward")
    if type(reward) not in (int, float) or not math.isfinite(reward) or reward not in (0, 1):
        raise TrainingRejected("A measured binary task outcome is required")
    if measured.get("reward_version") != REWARD_VERSION:
        raise TrainingRejected("Unrecognized Generator task-reward attribution")
    if not measured.get("evaluation_ref") or execution.get("evaluation_ref") != measured["evaluation_ref"]:
        raise TrainingRejected("Downstream verifier receipt is missing or changed")
    if skill is not None and (not measured.get("executor_trace_ref") or execution.get("executor_trace_ref") != measured["executor_trace_ref"]):
        raise TrainingRejected("Frozen Executor trajectory is missing or changed")
    if record.get("generation_status") == "invalid_skill" and reward != 0:
        raise TrainingRejected("Invalid skill output cannot claim a successful task outcome")
    messages, prompt = record.get("message_log"), record.get("input_message_log")
    if not isinstance(messages, list) or not isinstance(prompt, list) or not prompt or messages[:len(prompt)] != prompt:
        raise TrainingRejected("Captured prompt must be an exact message-log prefix")
    prompt_ids = []
    for message in prompt:
        if message.get("role") not in ("system", "developer", "user", "tool") or message.get("actor") != "context":
            raise TrainingRejected("Generator prompt contains a generated or Executor message")
        prompt_ids.extend(tokens(message))
    response_ids, masks, probabilities = [], [], []
    for message in messages[len(prompt):]:
        role, actor = message.get("role"), message.get("actor")
        original = tokens(message)
        if actor == "executor":
            raise TrainingRejected("Executor trace must remain behind the resources boundary")
        if role == "assistant":
            if actor != "generator" or message.get("policy_id") != ids["generator_policy_id"]:
                raise TrainingRejected("Assistant message did not come from the Generator policy")
            lp = message.get("generation_logprobs")
            if not isinstance(lp, list) or len(lp) != len(original) or any(type(p) not in (int, float) or not math.isfinite(p) or p > 0 for p in lp):
                raise TrainingRejected("Actual Generator sampling log probabilities are required")
            masks.extend([1] * len(original))
            probabilities.extend(float(p) for p in lp)
        elif role in ("user", "tool") and actor == "context":
            masks.extend([0] * len(original))
            probabilities.extend([0.0] * len(original))  # Masked context, not invented sampling data.
        else:
            raise TrainingRejected("Unsupported message ownership")
        response_ids.extend(original)
    if not any(masks):
        raise TrainingRejected("No sampled Generator tokens")
    if len(prompt_ids) > prompt_limit or len(response_ids) > response_limit:
        raise TrainingRejected("Oversized captures are excluded instead of silently truncating the skill")
    return GeneratorTokens(prompt_ids, response_ids, masks, probabilities, float(reward),
                           ids["generator_policy_id"], ids["fixed_executor_id"], hashlib.sha256(canonical(record)).hexdigest())


def admit_capture(record: dict, signature: str, key: bytes, *, firewall, prompt_limit=4096, response_limit=4096) -> GeneratorTokens:
    expected = sign_capture(record, key)
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
        raise TrainingRejected("Capture authentication failed")
    origin = record.get("capture", {})
    if origin.get("kind") != "provider_response" or origin.get("model_requests", 0) <= 0:
        raise TrainingRejected("Scripted diagnostics and fixtures cannot enter training")
    if not origin.get("provider_request_id") or not origin.get("capture_id"):
        raise TrainingRejected("Missing actual sampling request/capture identity")
    firewall.check_training_record(record.get("provenance", {}))
    firewall.check_training_record(record.get("downstream_execution", {}).get("provenance", {}))
    return pack_generator_trace(record, prompt_limit=prompt_limit, response_limit=response_limit)


def recipe_payload(record: dict, admitted: GeneratorTokens) -> dict[str, Any]:
    """Only pass admitted Generator messages to the unchanged upstream converter."""
    # Guard against using a receipt generated from a different mutable record.
    again = pack_generator_trace(record, prompt_limit=len(admitted.prompt_ids), response_limit=len(admitted.response_ids))
    if again != admitted:
        raise TrainingRejected("Capture changed after admission")
    fields = ("role", "token_ids", "generation_logprobs")
    return {"message_log": [{k: m[k] for k in fields if k in m} for m in record["message_log"]],
            "input_message_log": [{k: m[k] for k in fields if k in m} for m in record["input_message_log"]],
            "full_result": {"reward": admitted.reward}, "training_target": "skill_generator",
            "fixed_executor_id": admitted.fixed_executor_id,
            # The recipe reads the reward and Generator tokens. Keep the downstream
            # trajectory/verification references for audit, never as policy tokens.
            "downstream_execution": {k: record["downstream_execution"][k] for k in (
                "task_id", "skill_sha256", "executor_id", "executor_trace_ref", "evaluation_ref")}}
