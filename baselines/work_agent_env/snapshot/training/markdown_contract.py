"""Generator-native token evidence and immutable Markdown rollout identities.

This is separate from the retained declarative skill-v1 NeMo contract. API usage,
Executor tokens and diagnostic fixtures never acquire training admission here.
"""
from copy import deepcopy
import math

from integrations.benchmark_tasks.contracts import digest, nonempty
from training.skill_contract import TrainingRejected


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {
            "run_id", "step", "policy_id", "model_revision", "tokenizer_revision"}:
        raise TrainingRejected("Invalid policy snapshot")
    for key in ("run_id", "policy_id", "model_revision", "tokenizer_revision"):
        nonempty(policy[key], key)
    if type(policy["step"]) is not int or policy["step"] < 0:
        raise TrainingRejected("Invalid policy step")
    return policy


def validate_sample(sample, *, policy, request_id):
    """The actual sampling adapter supplies IDs/logprobs; no re-tokenization fallback."""
    if not isinstance(sample, dict) or sample.get("schema_version") != "workagent-generator-sample-v1":
        raise TrainingRejected("Missing native Generator token capture")
    if sample.get("request_id") != request_id or sample.get("policy") != validate_policy(policy):
        raise TrainingRejected("Stale or incorrectly routed Generator sample")
    if sample.get("actor") != "skill_generator" or sample.get("kind") not in ("verl_native", "diagnostic"):
        raise TrainingRejected("Executor and remote API evidence cannot become Generator training tokens")
    for name in ("prompt_ids", "response_ids"):
        ids = sample.get(name)
        if not isinstance(ids, list) or not ids or any(type(token) is not int or token < 0 for token in ids):
            raise TrainingRejected("Actual nonempty sampling token IDs required")
    probabilities = sample.get("response_logprobs")
    if (not isinstance(probabilities, list) or len(probabilities) != len(sample["response_ids"])
            or any(type(p) not in (int, float) or not math.isfinite(p) or p > 0 for p in probabilities)):
        raise TrainingRejected("Actual per-token Generator sampling logprobs required")
    nonempty(sample.get("provider_request_id"), "sampling request identity")
    if not isinstance(sample.get("text"), str) or not sample["text"].strip():
        raise TrainingRejected("Missing decoded Generator output")
    if sample.get("finish_reason") not in ("stop", "eos") and not (
            sample.get("kind") == "verl_native" and sample.get("finish_reason") == "length"
            and sample.get("terminal_contract") == "skillos_budget_failure_retained"):
        raise TrainingRejected("Truncated or incomplete Generator output")
    digest(sample)
    return deepcopy(sample)


def rollout_identity(*, batch_id, group_id, slot, policy, request_id, downstream_sha256):
    if type(slot) is not int or slot < 0:
        raise TrainingRejected("Invalid group slot")
    return digest({"batch_id": batch_id, "group_id": group_id, "slot": slot,
                   "policy": validate_policy(policy), "request_id": request_id,
                   "downstream_sha256": downstream_sha256})


def native_prompt(request):
    """Render tool definitions as context: the sampled action is a JSON edit array.

    No task tools are executable by the Generator, and no inference-time function
    transport or normalized response is substituted for the sampled token stream.
    """
    import json
    body = {key: value for key, value in request.items() if key != "id"}
    if digest(body) != request["id"]:
        raise TrainingRejected("Editor request changed")
    messages = deepcopy(request["messages"])
    messages[0]["content"] += "\n\nAvailable skill edit operation schemas:\n" + json.dumps(
        request["tools"], ensure_ascii=False, sort_keys=True, allow_nan=False)
    messages[0]["content"] += (
        "\n\nOutput contract: The entire response is a raw JSON array. Start with [ and end with ]. "
        "Do not use Markdown code fences or explanatory text outside the array. "
        "Each array item contains exactly name and arguments. For a new lesson, use new_skill_insert. "
        "A measured failure can teach a reusable prevention or recovery workflow. "
        "Return [] only when no reusable lesson can be extracted.\n"
        "Syntax example only (derive your own skill from the supplied experience; do not copy this example):\n"
        + json.dumps([{"name": "new_skill_insert", "arguments": {
            "skill_name": "short-name", "content": "---\nname: short-name\ndescription: Describe the reusable lesson.\n---\n# Workflow\nDescribe the steps learned from the supplied experience."}}])
    )
    return messages


def prompt_messages(value):
    """verl collates list-valued prompts into object arrays; preserve exact message values."""
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        raise TrainingRejected("Invalid collated Generator prompt")
    return deepcopy(list(value))
