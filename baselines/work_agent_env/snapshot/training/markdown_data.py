"""Review-bound seed/downstream pairs for the Markdown skill training path."""
from copy import deepcopy
import json

from integrations.benchmark_tasks.contracts import digest, identifier, validate_task
from training.markdown_contract import native_prompt
from training.seed_contract import prompt_digest
from training.skill_contract import TrainingRejected


def prepare_group(pair, *, editor, policy_id, training=False, firewall=None):
    required = {"schema_version", "group_id", "experience_id", "downstream_task", "parent",
                "seed_provenance", "downstream_provenance"}
    if not isinstance(pair, dict) or not required <= set(pair) or set(pair) - required - {"view_budget", "selfjudge_feedback"} or pair["schema_version"] != "workagent-training-pair-v1":
        raise TrainingRejected("Invalid training pair schema")
    identifier(pair["group_id"])
    record = editor.experience_store.get(pair["experience_id"])
    source, task = record["task"], validate_task(pair["downstream_task"])
    if source["uid"] == task["uid"]:
        raise TrainingRejected("Seed task cannot supply its own downstream reward")
    budget = pair.get("view_budget", {"max_steps": 100, "observation_chars": 12000})
    if not isinstance(budget, dict) or set(budget) != {"max_steps", "observation_chars"}:
        raise TrainingRejected("Invalid reviewed view budget")
    request = editor.prepare(pair["experience_id"], generator_policy_id=policy_id, parent=pair["parent"],
                             **budget, **({"selfjudge_feedback": pair["selfjudge_feedback"]} if "selfjudge_feedback" in pair else {}))
    if request["view"]["result"]["status"] != "measured":
        raise TrainingRejected("Unknown seed experience is quarantined")
    if training:
        if firewall is None:
            raise TrainingRejected("Training pairs require independent lineage review")
        seed, downstream = pair["seed_provenance"], pair["downstream_provenance"]
        firewall.check_training_record(seed)
        firewall.check_training_record(downstream)
        for provenance, actual, seed_sha in ((seed, source, digest(record)), (downstream, task, digest(task))):
            if (provenance["task_id"] != actual["task_id"] or provenance["seed_sha256"] != seed_sha
                    or provenance["source_commit"] != actual["source"]["revision"].split("+", 1)[0]):
                raise TrainingRejected("Reviewed lineage differs from actual task/experience bytes")
        if seed["prompt_sha256"] != prompt_digest(source["prompt"], {"input": native_prompt(request)}):
            raise TrainingRejected("Generator context differs from the reviewed input")
        if downstream["prompt_sha256"] != prompt_digest(task["prompt"], {"input": [{"role": "user", "content": task["prompt"]}]}):
            raise TrainingRejected("Downstream task prompt differs from its review")
    return {"group_id": pair["group_id"], "request": request, "downstream_task": deepcopy(task),
            "pair": deepcopy(pair),
            "seed_provenance": deepcopy(pair["seed_provenance"]),
            "downstream_provenance": deepcopy(pair["downstream_provenance"])}


def read_pairs(path):
    with open(path, encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if not rows or len({row["group_id"] for row in rows}) != len(rows):
        raise TrainingRejected("Empty pair dataset or duplicate group IDs")
    return rows
