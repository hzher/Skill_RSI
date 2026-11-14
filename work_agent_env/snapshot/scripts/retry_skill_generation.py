"""One diagnostic Generator request, optionally comparing only the system template."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.benchmark_tasks.store import save
from integrations.benchmark_tasks.contracts import digest
from integrations.executors import ExecutorOptions
from integrations.executors.models import APIModel
from integrations.executors.diagnostics import error_details
from integrations.experience import ExperienceStore
from integrations.skill_generator.editor import SkillEditor
from integrations.skill_generator.vendor.prompts import CURATOR_SYSTEM
from integrations.skill_library import SkillLibrary
from scripts.run_skill_pipeline import load_credentials


async def execute(args, request, receipt):
    model = APIModel(ExecutorOptions(provider=args.provider, model=args.model, max_output_tokens=8192))
    library = SkillLibrary(ROOT / ".runtime/skill_library")
    editor = SkillEditor(ExperienceStore(ROOT / ".runtime/experience"), library)
    try:
        async with asyncio.timeout(600):
            reply = await model.turn(request["messages"][0]["content"],
                [tool["function"] for tool in request["tools"]], [request["messages"][1]])
        save(args.output / "provider_reply.json", reply)
        calls = reply.get("calls", [])
        response = json.dumps([{"name": call["name"], "arguments": call["arguments"]} for call in calls], ensure_ascii=False) if calls else reply["text"]
        (args.output / "operations.json").write_text(response)
        candidate = editor.apply(request, response, sampling={"kind": "api_response", "reply": reply})
        save(args.output / "candidate.json", candidate)
        skills = library.get(candidate["version"])
        inventory = []
        for name, content in sorted(skills.items()):
            file = "skills/" + hashlib.sha256(name.encode()).hexdigest()[:16] + ".md"
            target = args.output / file
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x") as stream:
                stream.write(content)
            inventory.append({"name": name, "file": file})
        save(args.output / "skill_inventory.json", inventory)
        receipt.update(status="generated" if candidate["version"] != candidate["parent"] else "no_change",
            candidate_id=candidate["id"], version=candidate["version"], skill_count=len(skills),
            provider_response_id=reply.get("provider_response_id"), usage=reply.get("usage"))
    except Exception as exc:
        receipt.update(status="generation_failed", error=error_details(exc))
    finally:
        if getattr(model, "last_request", None) is not None:
            save(args.output / "request_diagnostics.json", model.last_request)
        await model.close()
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(args.output / "receipt.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=("openai", "anthropic"), default="openai")
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--legacy-system", action="store_true", help="Record a system-only contrast using the original proven template")
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    source_request_id = request["id"]
    if args.legacy_system:
        body = {key: value for key, value in request.items() if key != "id"}
        body["messages"][0]["content"] = CURATOR_SYSTEM + "\nReturn only a raw JSON array of {name,arguments} tool operations, or []. Unknown results are not failures."
        body["protocol_variant"] = "original-system-with-explicit-namespace-adapter"
        request = {"id": digest(body), **body}
    if not args.execute:
        print(json.dumps({"status": "planned_not_executed", "request_id": request["id"]}))
        return
    load_credentials(ROOT / ".runtime/private/gpt-api.env")
    args.output.mkdir(parents=True, exist_ok=False)
    save(args.output / "request.json", request)
    receipt = {"schema_version": "workagent-generator-diagnostic-retry-v1", "status": "model_request",
        "started_at": datetime.now(timezone.utc).isoformat(), "task_uid": request["task_uid"],
        "request_id": request["id"], "source_request_id": source_request_id,
        "source_request": str(args.request), "unchanged_input": not args.legacy_system,
        "task_context_unchanged": True, "legacy_system_contrast": args.legacy_system,
        "provider": args.provider, "model": args.model, "logical_requests": 1,
        "task_execution_repeated": False, "training_admitted": False,
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in
            [Path(__file__), ROOT / "integrations/skill_generator/editor.py", ROOT / "integrations/skill_generator/transport.py"]}}
    save(args.output / "receipt.json", receipt)
    asyncio.run(execute(args, request, receipt))


if __name__ == "__main__":
    main()
