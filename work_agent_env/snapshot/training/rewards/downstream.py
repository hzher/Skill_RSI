"""Only bound original downstream task outcomes supply the default reward."""
from copy import deepcopy
import hmac

from integrations.benchmark_tasks.contracts import digest
from integrations.skill_generator.attributor import attribute
from training.markdown_contract import validate_sample
from training.skill_contract import TrainingRejected, sign_capture


def bind_reward(*, candidate, execution, sample, policy, request_id, executor_id):
    original_sample = validate_sample(sample, policy=policy, request_id=request_id)
    if candidate["provenance"]["request"]["id"] != request_id:
        raise TrainingRejected("Candidate belongs to a different Generator input")
    if candidate["provenance"]["generator_policy_id"] != policy["policy_id"]:
        raise TrainingRejected("Candidate policy changed")
    if candidate["provenance"]["response"] != original_sample["text"]:
        raise TrainingRejected("Skill edits differ from sampled Generator output")
    attribution = attribute(candidate, execution, fixed_executor_id=executor_id)
    if attribution["mask_sample"] or attribution["reward"] is None:
        raise TrainingRejected("Unknown downstream result cannot enter a policy update")
    return {"schema_version": "workagent-markdown-rollout-v1", "sample": original_sample,
            "candidate_id": candidate["id"], "skill_version": candidate["version"],
            "attribution": attribution, "reward": attribution["reward"],
            "execution_sha256": digest(execution), "training_admitted": False}


def seal_groups(rows, *, expected, policy, algorithm, mode, firewall=None, capture_key=None, roles_config=None):
    """Explicit slot ordering, complete groups and one frozen policy per update.

    expected is group_id -> ordered rollout identities. rows may finish in any
    order. Training provenance must have been reviewed for seed and downstream.
    """
    if mode not in ("diagnostic", "training") or not expected:
        raise TrainingRejected("Invalid batch mode or empty groups")
    observed = {}
    capture_ids = set()
    group_prompts = {}
    executor_ids = set()
    for row in rows:
        identity = row["rollout_id"]
        group = row["group_id"]
        if identity in observed or group not in expected or identity not in expected[group]:
            raise TrainingRejected("Duplicated or unexpected rollout")
        sample = row["sample"]
        validate_sample(sample, policy=policy, request_id=row["request_id"])
        if sample["provider_request_id"] in capture_ids:
            raise TrainingRejected("Sampling request replayed across group slots")
        capture_ids.add(sample["provider_request_id"])
        prompt_key = digest({"request": row["request_id"], "tokens": sample["prompt_ids"],
                             "downstream": row["downstream_sha256"], "parent": row["parent"]})
        if group_prompts.setdefault(group, prompt_key) != prompt_key:
            raise TrainingRejected("GRPO group mixes prompts, parents or downstream tasks")
        reward = row["reward"]
        import math
        if type(reward) not in (int, float) or not math.isfinite(reward):
            raise TrainingRejected("Invalid measured reward")
        if mode == "training":
            if sample["kind"] != "verl_native" or firewall is None or capture_key is None:
                raise TrainingRejected("Only native Generator sampling and reviewed provenance can train")
            body = {key: value for key, value in row.items() if key != "signature"}
            if not hmac.compare_digest(sign_capture(body, capture_key), row.get("signature", "")):
                raise TrainingRejected("Generator/downstream capture authentication failed")
            firewall.check_training_record(row["seed_provenance"])
            firewall.check_training_record(row["downstream_provenance"])
        observed[identity] = deepcopy(row)
        attribution = row["attribution"]
        composite = row.get("skillos_reward")
        if composite is not None:
            from integrations.skill_generator.roles import validate_reward
            try:
                validate_reward(composite, row=row, config=roles_config)
            except (ValueError, KeyError, TypeError) as exc:
                raise TrainingRejected("Invalid bound SkillOS composite reward") from exc
        if (row["slot"] != expected[group].index(identity)
                or (composite is None and attribution["reward"] != row["reward"]) or attribution["mask_sample"] is not False
                or attribution["candidate_id"] != row["candidate_id"] or attribution["skill_version"] != row["skill_version"]
                or attribution["generator_policy_id"] != policy["policy_id"]):
            raise TrainingRejected("Rollout slot, sampled policy or attributed reward changed")
        executor_ids.add(attribution["fixed_executor_id"])
    if len(executor_ids) != 1:
        raise TrainingRejected("A batch cannot mix frozen Executors")
    ordered, audits = [], []
    for group, identities in expected.items():
        if len(identities) != algorithm.group_size or len(set(identities)) != len(identities):
            raise TrainingRejected("Invalid group cardinality")
        if any(identity not in observed for identity in identities):
            raise TrainingRejected("Incomplete batch; no optimizer update is permitted")
        members = [observed[identity] for identity in identities]
        advantages = algorithm.advantages([member["reward"] for member in members])
        ordered.extend(members)
        audits.append({"group_id": group, "rollout_ids": identities, "rewards": [member["reward"] for member in members],
                       "audit_advantages": advantages, "has_learning_signal": any(value != 0 for value in advantages)})
    if len(ordered) != len(rows):
        raise TrainingRejected("Unexpected extra rows")
    body = {"schema_version": "workagent-sealed-rollout-batch-v1", "mode": mode, "policy": deepcopy(policy),
            "algorithm": algorithm.name, "rows": ordered, "groups": audits,
            "training_admitted": mode == "training"}
    return {"id": digest(body), "signature": sign_capture(body, capture_key) if mode == "training" else None, **body}
