"""Read-only validation and selection of versioned local task resources."""
from __future__ import annotations
import hashlib
import json
import re
from workenv import core
from workenv.core import contained,file_snapshot,read_json,no_links

WORKFLOWS={"receivables":"invoice_payment_reconciliation","meeting":"multi_person_meeting_scheduling","documents":"policy_revision_review_with_citations"}
WORKFLOWS["tickets"]="ticket_routing_with_approval"
SPLITS={"dev","train","validation","test"}


def tasks(*,family=None,split=None,runnable=False):
    return [t for t in read_json(core.ROOT/"tasks/registry.json")["tasks"] if (family is None or t["family"]==family)
            and (split is None or t["split"]==split) and (not runnable or bool(t.get("runtime_adapter")))]


def validate_pack(entry):
    if entry.get("runtime_adapter") not in ("local_files","local_tickets"):raise ValueError("Local pack validation requires an isolated local adapter")
    base=contained(core.ROOT,entry["source_path"])
    manifest=read_json(base/"task.json")
    if not isinstance(manifest,dict):raise ValueError("Task manifest must be an object")
    if any(entry.get(key)!=value for key,value in manifest.items()):raise ValueError("Registry and task manifest disagree")
    required={"task_id","family","workflow_id","world_id","leakage_group_id","split","variant","seed","task_version","runtime_adapter","source","license","split_status","asset_sha256","content_sha256","world_content_sha256"}
    if not required<=manifest.keys():raise ValueError("Task manifest is incomplete")
    if not isinstance(manifest["task_id"],str) or not re.fullmatch(r"[A-Za-z0-9_-]+",manifest["task_id"]):raise ValueError("Invalid task identifier")
    if manifest["split"] not in SPLITS or manifest["split_status"]!="construction_candidate_not_frozen":raise ValueError("Unsupported split or freeze claim")
    if manifest["family"] not in WORKFLOWS or manifest["workflow_id"]!=WORKFLOWS[manifest["family"]]:raise ValueError("Unsupported family/workflow relationship")
    if manifest["runtime_adapter"]!=("local_tickets" if manifest["family"]=="tickets" else "local_files"):raise ValueError("Adapter does not match task family")
    if type(manifest["seed"]) is not int or not 0<=manifest["seed"]<=100000:raise ValueError("Invalid seed")
    if manifest["variant"] not in ("base","changed_constraint"):raise ValueError("Invalid variant")
    if manifest["task_id"]!=f"local-{manifest['family']}-{manifest['seed']}-{manifest['variant']}" or manifest["world_id"]!=f"{manifest['family']}-{manifest['seed']}":raise ValueError("Identifier/seed/world mismatch")
    assets=manifest["asset_sha256"]
    if not isinstance(assets,dict) or "task.md" not in assets or "inputs/policy.json" not in assets:raise ValueError("Missing task assets")
    for name,value in assets.items():
        if not isinstance(name,str) or (name!="task.md" and not name.startswith("inputs/")):raise ValueError("Task asset is outside public inputs")
        if ":" in name or "\\" in name or any(p.startswith(".") for p in name.split("/")):raise ValueError("Invalid task asset path")
        contained(base,name)
        if not isinstance(value,str) or not re.fullmatch(r"[0-9a-f]{64}",value):raise ValueError("Invalid asset digest")
    listed={}
    for path in base.rglob("*"):
        no_links(path,base)
        if path.is_file():listed[path.relative_to(base).as_posix()]=path
    if set(listed)!=set(assets)|{"task.json"}:raise ValueError("Task pack contains missing or untracked resources")
    if any(p.stat().st_size>512*1024 for p in listed.values()):raise ValueError("Task input exceeds file tool budget")
    actual=file_snapshot(base)
    if set(actual)!=set(assets)|{"task.json"}:raise ValueError("Task pack contains missing or untracked resources")
    for name,value in assets.items():
        if actual[name]!=value:raise ValueError("Task resource hash mismatch: "+name)
        if contained(base,name).stat().st_size>512*1024:raise ValueError("Task input exceeds file tool budget")
    if not (base/"task.md").read_text(encoding="utf-8").strip():raise ValueError("Empty task instruction")
    inputs={name.removeprefix("inputs/"):contained(base,name).read_text(encoding="utf-8") for name in assets if name.startswith("inputs/")}
    if not isinstance(json.loads(inputs["policy.json"]),dict):raise ValueError("Policy must be an object")
    from workenv.input_validation import validate_inputs
    validate_inputs(manifest["family"],inputs)
    content_hash=hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()
    world_hash=hashlib.sha256(json.dumps({k:v for k,v in inputs.items() if k!="policy.json"},sort_keys=True).encode()).hexdigest()
    if content_hash!=manifest["content_sha256"] or world_hash!=manifest["world_content_sha256"]:raise ValueError("Declared content/world digest differs from actual inputs")
    if manifest["leakage_group_id"]!=manifest["workflow_id"]+":"+world_hash:raise ValueError("Leakage group does not describe actual world")
    return {"task_id":manifest["task_id"],"family":manifest["family"],"split":manifest["split"],"assets":len(assets),"content_sha256":content_hash,"world_content_sha256":world_hash,"passed":True}


def audit_registry():
    entries=tasks()
    ids=set()
    groups={}
    contents=set()
    worlds={}
    files=resources=0
    for entry in entries:
        if entry["task_id"] in ids:raise ValueError("Duplicate task ID")
        ids.add(entry["task_id"])
        if entry["split"] not in SPLITS:raise ValueError("Invalid registry split")
        group=entry["leakage_group_id"]
        if group in groups and groups[group]!=entry["split"]:raise ValueError("Leakage group spans splits")
        groups[group]=entry["split"]
        if entry.get("runtime_adapter") in ("local_files","local_tickets"):
            result=validate_pack(entry)
            if result["content_sha256"] in contents:raise ValueError("Duplicate task contents")
            contents.add(result["content_sha256"])
            world=result["world_content_sha256"]
            if world in worlds and worlds[world]!=entry["split"]:raise ValueError("Identical input worlds cross splits")
            worlds[world]=entry["split"]
            if entry["runtime_adapter"]=="local_files":files+=1
            else:resources+=1
    result={"tasks":len(entries),"local_file_tasks":files,"leakage_groups":len(groups),"passed":True,"splits_are_frozen":False,"blind_test_claimed":False}
    if resources:result.update(local_resource_tasks=resources,local_task_packs=len(contents))
    return result
