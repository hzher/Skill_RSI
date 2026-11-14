"""Shared raw-HTTP, retained-measurement and lifecycle checks for office tasks."""
from collections import Counter
import hashlib
import json

from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_input, track_absence
from integrations.runtime_identity import expected_identity
from workenv.core import contained

def audit_report(root, path, *, family, task_id, checks, extra_sources, episode_result, audit_measurement,
                 check_inventory, reward_version, reference_mode, min_blindspots=2):
    receipt = read_json(path, root)
    if (receipt.get("schema_version") != "nemo-" + family + "-project-diagnostic-v1" or receipt.get("passed") is not True
        or receipt.get("execution_kind") != "scripted_original_task_diagnostic"
        or receipt.get("cleanup_errors") != [] or [c["name"] for c in receipt["checks"]] != list(checks)
        or any(c["passed"] is not True for c in receipt["checks"]) or receipt.get("reference_mode") != reference_mode
        or any(receipt.get(k) is not False for k in ("training_eligible", "upstream_reference_used", "official_benchmark_score_available", "reference_solutions_read", "verified_task_bodies_read"))
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates", "training_admissions"))):
        raise ValueError("Office report NeMo diagnostic scope differs")
    seeds = receipt.get("seeds", [])
    if (len(seeds) != 3 or len({v["resources_session_id"] for v in seeds}) != 3
        or any(v["task_id"]["task_id"] != task_id for v in seeds[:2])
        or seeds[2]["task_id"]["task_id"] != "howtocook-meal-plan-gcal" or receipt.get("error") is not None):
        raise ValueError("Office report task/resource scope differs")
    sources = {**expected_identity(root), **{n: hashlib.sha256((root/n).read_bytes()).hexdigest() for n in extra_sources}}
    if receipt["source_sha256"] != sources: raise ValueError("Office report NeMo source inventory differs")
    source_binding(root, sources)
    retained = receipt["retained_sha256"]
    if not 20 <= len(retained) <= 512: raise ValueError("Office report NeMo retained inventory budget")
    for name, checksum in retained.items():
        member = contained(root, name)
        if member.stat().st_size > 16 * 1024 * 1024 or hashlib.sha256(member.read_bytes()).hexdigest() != checksum:
            raise ValueError("Office report NeMo retained bytes differ")
        track_input(member, root, checksum)
    journal = read_json(path.parent / "http_journal.json", root, limit=16*1024*1024)
    indices = {s["resources_session_id"]: i for i,s in enumerate(receipt["seeds"])}
    raw_measurements = []
    for event in journal:
        if event["route"] != "/isolated/verify" or event["status"] != 200: continue
        request, reply = event["request"], event["response"]
        sid = request["verification_input"]["resources_session_id"]
        if (sid not in indices or reply["episode_id"] != request["episode_id"] or reply["task_id"] != request["task_id"]
            or request["episode_id"] != receipt["seeds"][indices[sid]]["episode_id"]
            or request["task_id"] != receipt["seeds"][indices[sid]]["task_id"]):
            raise ValueError("Office report verification identity differs")
        raw_measurements.append({"index": indices[sid], "result": reply["result"]})
    encode = lambda v: json.dumps(v, sort_keys=True, separators=(",", ":"))
    if Counter(map(encode,raw_measurements)) != Counter(map(encode,receipt["measurements"])):
        raise ValueError("Office report measurement summary differs from raw HTTP replies")
    count, blind, snapshots = 0, 0, {}
    for row in receipt["measurements"]:
        if row["index"] > 1: continue
        value = row["result"]
        if value["mask_sample"]:
            if value["reward"] is not None: raise ValueError("Unknown fulfillment result acquired reward")
            continue
        if type(value["reward"]) not in (int, float): raise ValueError("Office report reward type differs")
        if value["reward_version"] != reward_version: raise ValueError("Office report reward version differs")
        ref = value["evaluation_ref"]
        if ref not in retained or read_json(contained(root,ref),root) != value["evaluation"]:
            raise ValueError("Office report runtime evaluator summary differs")
        evaluated = value["evaluation"]
        component_ref = evaluated["component_evaluation_ref"]
        if component_ref not in retained: raise ValueError("Office report component measurement is not retained")
        component = read_json(contained(root,component_ref),root)
        checked = audit_measurement(root,component)
        if (evaluated != episode_result(component) or evaluated["reference_mode"] != reference_mode
            or value["reward"] != float(checked["component"])):
            raise ValueError("Office report project-derived reward or mode differs")
        snapshots.setdefault(row["index"], checked["data"])
        if snapshots[row["index"]]["readonly_sha256"] != checked["data"]["readonly_sha256"]: raise ValueError("Office report source changed")
        blind += int(checked["original"] is True and checked["supplemental"]["supplemental_passed"] is False)
        count += 1
    if count < 8 or blind < min_blindspots or receipt["shared_before"] != receipt["shared_after"]:
        raise ValueError("Incomplete fulfillment NeMo cases")
    for i, record in receipt["profiles"].items():
        profile = EpisodeProfile(root,record["resource_id"],record["slot"]); profile.validate_record(record)
        require_dependents_closed(profile); track_absence(profile.data / "postmaster.pid",root)
        if int(i) > 1: continue
        from integrations.task_routing import TaskRouter
        seed = receipt["seeds"][int(i)]
        run = profile.base / (family + "-task-session")
        if read_json(run / "task_profile.json",root) != TaskRouter(root,"isolated").authorize(seed):
            raise ValueError("Office report runtime profile differs")
        if read_json(profile.base / "resources-session.json",root)["sessions"][profile.resource_id]["state"] != "closed":
            raise ValueError("Office report worker did not close")
        check_inventory(root, profile, snapshots[int(i)])
    return {"checks":len(checks), "evaluations_recomputed":count,"reference_mode":reference_mode,"official_score_available":False,"training_admissions":0}


def audit_lifecycle(root,path, *, family, task_id, expected_children=("woocommerce","google_sheet")):
    from integrations.isolated_audit import audit_isolated, CHECKS as BASE_CHECKS
    from integrations.task_routing import TaskRouter
    mapping={"nemo-"+family+"-recovery-diagnostic-v1":BASE_CHECKS["nemo-isolated-recovery-diagnostic-v1"],
             "nemo-"+family+"-expiry-diagnostic-v1":BASE_CHECKS["nemo-isolated-expiry-diagnostic-v1"]}
    receipt=read_json(path,root)
    if receipt.get("task_id") != task_id: raise ValueError("Office report lifecycle task differs")
    result=audit_isolated(root,path,expected_task=task_id,checks_by_schema=mapping)
    journal=read_json(path.parent/"http_journal.json",root)
    seeds=[e["request"] for e in journal if e["route"]=="/isolated/seed_session" and e["status"]==200]
    if len(seeds)!=(2 if "recovery" in receipt["schema_version"] else 1): raise ValueError("Office report lifecycle seed count differs")
    for seed in seeds:
        base=root/".runtime/native-episodes"/seed["resources_session_id"]
        owner=read_json(base/"cluster_owner.json",root)
        profile=EpisodeProfile(root,seed["resources_session_id"],owner["slot"]); profile.validate_record(owner)
        if read_json(base/(family+"-task-session/task_profile.json"),root)!=TaskRouter(root,"isolated").authorize(seed): raise ValueError("Office report lifecycle profile differs")
        require_dependents_closed(profile); track_absence(profile.data/"postmaster.pid",root)
        if set(read_json(base/"dependent_processes.json",root)["children"])!=set(expected_children): raise ValueError("Office report lifecycle dependency inventory differs")
        if read_json(base/"resources-session.json",root)["sessions"][profile.resource_id]["state"]!="closed": raise ValueError("Office report lifecycle worker not closed")
    if "expiry" in receipt["schema_version"] and receipt.get("normal_lease_restored_seconds")!=900: raise ValueError("Office report lease not restored")
    return {**result,"original_"+family+"_episodes":len(seeds)}
