"""Bind reviewed seed provenance to the exact Generator prompt and task request."""
import hashlib
import json

from training.skill_contract import TrainingRejected

SAMPLING_FIELDS = {"model", "temperature", "top_p", "top_k", "max_output_tokens", "parallel_tool_calls"}


def prompt_digest(task_prompt, responses_create_params):
    request = {key: value for key, value in responses_create_params.items() if key not in SAMPLING_FIELDS | {"stream"} and value is not None}
    if isinstance(request.get("input"), list):
        normalized = []
        for message in request["input"]:
            if set(message) - {"role", "content", "type"} or message.get("type", "message") != "message":
                raise TrainingRejected("Unsupported Generator input message metadata")
            normalized.append({"role": message["role"], "content": message["content"]})
        request["input"] = normalized
    encoded = json.dumps({"task_prompt": task_prompt, "request": request}, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_seed_row(row, firewall):
    allowed = {"provenance", "training_target", "agent_ref", "responses_create_params", "task_prompt",
               "downstream_provenance", "downstream_task_prompt"}
    if not isinstance(row, dict) or set(row) != allowed:
        raise TrainingRejected("Generator seed row contains missing/unsupported fields")
    if row["training_target"] != "skill_generator" or row["agent_ref"] != {"type": "responses_api_agents", "name": "skill_generator"}:
        raise TrainingRejected("Seed does not route exclusively to Skill Generator")
    firewall.check_training_record(row["provenance"])
    downstream = row["downstream_provenance"]
    firewall.check_training_record(downstream)
    if downstream["task_id"] == row["provenance"]["task_id"]:
        raise TrainingRejected("Generator experience and Executor evaluation must use distinct tasks")
    downstream_prompt = row["downstream_task_prompt"]
    if (not isinstance(downstream_prompt, str) or not 0 < len(downstream_prompt) <= 32000
            or downstream["prompt_sha256"] != prompt_digest(downstream_prompt, {"input": [{"role": "user", "content": downstream_prompt}]})):
        raise TrainingRejected("Downstream task prompt differs from reviewed task")
    prompt, request = row["task_prompt"], row["responses_create_params"]
    if not isinstance(prompt, str) or not 0 < len(prompt) <= 32000 or not isinstance(request, dict):
        raise TrainingRejected("Invalid task prompt or Generator request")
    messages = request.get("input")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 8 or request.get("tools"):
        raise TrainingRejected("Generator seed must be a bounded prompt without direct task tools")
    for message in messages:
        if set(message) != {"role", "content"} or message["role"] not in ("system", "developer", "user") or not isinstance(message["content"], str) or not 0 < len(message["content"]) <= 32000:
            raise TrainingRejected("Unsupported seed prompt message")
    if row["provenance"].get("prompt_sha256") != prompt_digest(prompt, request):
        raise TrainingRejected("Prompt differs from the independently reviewed seed")
    return row
