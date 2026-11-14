"""Experience -> recorded Curator request -> strict candidate; no implicit publication."""
from copy import deepcopy
import json

from integrations.benchmark_tasks.contracts import digest, nonempty
from .handlers import apply_operations
from .transport import normalize_response
from .vendor.prompts import CURATOR_SYSTEM, CURATOR_INPUT_TEMPLATE, CURATOR_TOOLS, format_trajectory


class SkillEditor:
    def __init__(self, experience_store, library):
        self.experience_store, self.library = experience_store, library

    def prepare(self, experience_id, *, generator_policy_id, parent=None, top_k=5,
                max_steps=100, observation_chars=12000, selfjudge_feedback=None):
        nonempty(generator_policy_id, "Generator policy identity")
        view = self.experience_store.view(experience_id, max_steps=max_steps, observation_chars=observation_chars)
        parent = parent or self.library.head() or self.library.empty_version
        selected = self.library.retrieve(view["task_description"], version=parent, top_k=top_k)
        past = "\n\n".join("## " + row["name"] + "\n" + row["content"] for row in selected) or "(none)"
        user = CURATOR_INPUT_TEMPLATE.format(task_description=view["task_description"], past_skills=past,
            agent_trajectory=format_trajectory(view["steps"]) or "No tool observation recorded.",
            result=json.dumps(view["result"], ensure_ascii=False))
        if selfjudge_feedback is not None:
            user += "\n\n## Frozen SelfJudge feedback (original evaluator remains authoritative benchmark evidence)\n" + json.dumps(selfjudge_feedback, ensure_ascii=False)
        request = {"schema_version": "workagent-editor-request-v1", "experience_id": experience_id,
                   "task_uid": view["task_uid"], "parent": parent, "generator_policy_id": generator_policy_id,
                   "messages": [{"role": "system", "content": CURATOR_SYSTEM +
                       "\nReturn only a raw JSON array of {name,arguments} tool operations, or []. Unknown results are not failures."},
                       {"role": "user", "content": user}], "tools": deepcopy(CURATOR_TOOLS),
                   "view": view, "training_admitted": False}
        return {"id": digest(request), **request}

    def apply(self, request, response, *, sampling=None, record_failures=False):
        body = {key: value for key, value in request.items() if key != "id"}
        if digest(body) != request["id"]:
            raise ValueError("Editor request changed")
        if record_failures:
            from .vendor.reward_toolchain import SkillRepo, parse_calls
            repo = SkillRepo(self.library.get(request["parent"]))
            try:
                normalized, transport = normalize_response(response)
                calls = parse_calls(normalized)
            except (ValueError, TypeError) as exc:
                normalized, transport = response, {"adapter": "skillos-invalid-output-retained"}
                calls = [{"raw": None, "parse_error": type(exc).__name__ + ": " + str(exc)[:200]}]
            results = [repo.apply(call) for call in calls]
            skills = repo.skills
        else:
            normalized, transport = normalize_response(response)
            skills, results = apply_operations(self.library.get(request["parent"]), normalized)
        return self.library.candidate(parent=request["parent"], skills=skills, operations=results,
            provenance={"experience_id": request["experience_id"], "seed_task_uid": request["task_uid"],
                        "generator_policy_id": request["generator_policy_id"], "request": request,
                        "response": response, "sampling": deepcopy(sampling),
                        "normalized_response": normalized, "transport": transport,
                        "response_sha256": digest(response), "training_admitted": False})

    async def generate(self, experience_id, *, model, generator_policy_id, parent=None):
        """APIModel-compatible transport; usage is evidence, never fabricated token/logprob capture."""
        request = self.prepare(experience_id, generator_policy_id=generator_policy_id, parent=parent)
        reply = await model.turn(request["messages"][0]["content"],
            [tool["function"] for tool in request["tools"]], [request["messages"][1]])
        calls = reply.get("calls", [])
        response = json.dumps([{"name": call["name"], "arguments": call["arguments"]} for call in calls], ensure_ascii=False) if calls else reply["text"]
        return self.apply(request, response, sampling={"kind": "api_response", "reply": reply})
