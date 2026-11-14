"""Accept completed Generator/downstream rollouts without invoking either model.

The collector owns authenticity of sampling and verifier receipts. This boundary
checks integrity and current-policy/request binding before the verl conversion.
It does not replay tasks, relabel old API traces, or compute a GRPO advantage.
"""
from copy import deepcopy
import hashlib

from training.skill_contract import TrainingRejected, admit_capture, canonical, recipe_payload


def prepare_rollout_batch(*, requests, captures, capture_key, firewall,
                          generator_policy_id, fixed_executor_id,
                          training_run_id, global_step,
                          prompt_limit=4096, response_limit=4096):
    """Return recipe payloads in request order for one already collected batch.

    Each capture is {record, signature}; each request binds provenance,
    downstream_provenance and episode_id. Ordering is explicit, never inferred
    from task names or whichever asynchronous task happened to finish first.
    """
    if not requests or len(requests) != len(captures):
        raise TrainingRejected("Incomplete Generator/downstream rollout batch")
    if type(global_step) is not int or global_step < 0 or not training_run_id:
        raise TrainingRejected("Missing current Generator training run/step")
    payloads, capture_ids, episodes = [], set(), set()
    for request, envelope in zip(requests, captures):
        if not isinstance(request, dict) or not isinstance(envelope, dict) or not isinstance(envelope.get("record"), dict):
            raise TrainingRejected("Missing structured Generator/downstream capture")
        record = deepcopy(envelope.get("record", {}))
        if (record.get("provenance") != request.get("provenance")
                or record.get("downstream_execution", {}).get("provenance") != request.get("downstream_provenance")):
            raise TrainingRejected("Generator sample or downstream reward belongs to a different request")
        task_prompt = request.get("downstream_task_prompt")
        if (not isinstance(task_prompt, str) or not task_prompt
                or record["downstream_execution"].get("task_prompt_sha256") != hashlib.sha256(task_prompt.encode("utf-8")).hexdigest()):
            raise TrainingRejected("Downstream trajectory belongs to a different task prompt")
        identities = record.get("identities", {})
        if (identities.get("generator_policy_id") != generator_policy_id
                or identities.get("fixed_executor_id") != fixed_executor_id):
            raise TrainingRejected("Generator policy or frozen Executor identity changed")
        origin = record.get("capture", {})
        capture_id = origin.get("capture_id")
        episode = canonical(request.get("episode_id"))
        if (origin.get("global_step") != global_step
                or origin.get("training_run_id") != training_run_id
                or not request.get("episode_id")
                or origin.get("episode_id") != request["episode_id"]
                or not isinstance(capture_id, str) or not capture_id
                or capture_id in capture_ids or episode in episodes):
            raise TrainingRejected("Stale, replayed or incorrectly bound Generator rollout")
        admitted = admit_capture(record, envelope.get("signature"), capture_key,
                                 firewall=firewall, prompt_limit=prompt_limit,
                                 response_limit=response_limit)
        capture_ids.add(capture_id)
        episodes.add(episode)
        payloads.append(recipe_payload(record, admitted))
    return payloads
