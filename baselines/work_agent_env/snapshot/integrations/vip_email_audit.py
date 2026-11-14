"""Independent recomputation of retained VIP business component evidence."""
import hashlib
from uuid import UUID

from adapters import vip_email_validation as validation
from adapters.vip_email_evaluation import classify_original, supplement
from integrations.vip_email_components import METHODS, jsonable, check_customers, validate_call, load_snapshot
from integrations.email_components import fingerprint,verify_effect
from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_input, track_absence
from training.email_task_review import BASE, PUBLIC_SHA256
from workenv.core import contained

CHECKS = ("no_nemo_task_activation", "two_owned_empty_resources", "ten_original_method_schemas", "complete_customer_pagination_and_independent_sql",
    "empty_outputs_measured_failure", "two_complete_business_outputs_pass", "same_request_deduplicates_and_conflict_refused", "foreign_cloud_and_mutations_refused",
    "cent_error_original_blind_spot_rejected", "duplicate_customer_original_blind_spot_rejected", "missing_send_log_original_blind_spot_rejected",
    "wrong_recipient_rejected", "extra_message_original_blind_spot_rejected", "restored_business_passes", "foreign_marker_keeps_measurement_unknown",
    "closing_first_keeps_second_active", "reset_first_preserves_second", "original_cloud_error_is_unknown", "unknown_blocks_replay_measurement_and_reset",
    "three_children_per_resource_closed", "owned_postgres_closed", "shared_database_and_nemo_ledgers_unchanged")
SOURCES = ("integrations/vip_email_components.py", "integrations/vip_email_audit.py", "adapters/vip_email_evaluation.py", "adapters/vip_email_validation.py",
    "scripts/verify_vip_email_components.py", "integrations/email_components.py", "integrations/email_build.py", "adapters/email_pg_native.py",
    "integrations/customer_report_components.py", "integrations/order_analysis_resource.py", "adapters/google_sheets_native.py", "adapters/woocommerce_native.mjs",
    "training/email_task_review.py")


def audit_measurement(root, measured):
    directory = contained(root, measured["evaluation_ref"]).parent
    profile=EpisodeProfile(root,measured['profile']['resource_id'],measured['profile']['slot']);profile.validate_record(measured['profile'])
    if (not directory.is_relative_to(profile.base/'vip-evaluations') or measured['snapshot_ref']!=(directory/'source_snapshot.json').relative_to(root).as_posix()
        or measured['original_customer_inventory_ref']!=(profile.base/'vip-components-run/original_customers.json').relative_to(root).as_posix()):
        raise ValueError('VIP measurement input belongs to another resource')
    if read_json(directory / "result.json",root) != measured: raise ValueError("VIP measurement summary changed")
    if hashlib.sha256((directory / "original_evaluation.py").read_bytes()).hexdigest() != PUBLIC_SHA256[BASE+"evaluation/main.py"]:
        raise ValueError("VIP evaluator differs from pinned original")
    if measured['original_source'].get('sha256')!=PUBLIC_SHA256[BASE+'evaluation/main.py']:raise ValueError('VIP evaluator identity changed')
    data = load_snapshot(root,contained(root,measured['snapshot_ref']),profile.base/'vip-components-run/initial_snapshot.json')
    original = classify_original(measured["original_exit_code"],(directory/"original.stdout.txt").read_text(),(directory/"original.stderr.txt").read_text())
    supplemental = supplement(data)
    source = contained(root,measured["original_customer_inventory_ref"])
    if hashlib.sha256(source.read_bytes()).hexdigest() != measured["original_customer_inventory_sha256"]: raise ValueError("VIP source inventory changed")
    check_customers(read_json(source,root)["customers"],data)
    truth = validation.expected(data["office"]["customers"],source_binding=validation.source_identity())
    sql = read_json(directory/"sql_truth.json",root)
    expected = jsonable(truth)
    if {r["Email"]:r for r in sql["rows"]} != {r["Email"]:r for r in expected["rows"]} or sql["metrics"] != expected["metrics"]:
        raise ValueError("VIP independent SQL/contract result differs")
    combined = None if original is None or supplemental["supplemental_passed"] is None else bool(original and supplemental["supplemental_passed"])
    if (original is not measured["original_passed"] or supplemental != measured["supplemental"] or combined is not measured["component_measurement_passed"]
        or measured.get("task_passed") is not None or measured.get("reward") is not None
        or measured.get("source_completeness_verified") is not True or measured.get("owned_database_snapshot_verified") is not True
        or any(measured.get(k) is not False for k in ("runtime_task_activated", "training_eligible", "upstream_reference_used", "official_benchmark_score_available"))
        or type(measured.get('external_messages_sent')) is not int or measured['external_messages_sent']!=0):
        raise ValueError("VIP component measurement recomputation differs")
    return original,supplemental["supplemental_passed"]


def audit_components(root,path):
    receipt = read_json(path,root)
    if (receipt.get("schema_version") != "vip-original-business-components-diagnostic-v1" or receipt.get("passed") is not True
        or [c["name"] for c in receipt["checks"]] != list(CHECKS) or any(c["passed"] is not True for c in receipt["checks"])
        or receipt.get("runtime_task_activated") is not False or receipt.get("task_passed") is not None or receipt.get("reward") is not None
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls","training_updates","training_admissions","external_messages_sent"))
        or set(receipt.get("source_sha256",{})) != set(SOURCES)):
        raise ValueError("VIP business diagnostic scope or checks differ")
    source_binding(root,receipt["source_sha256"])
    profiles = [EpisodeProfile(root,p["resource_id"],p["slot"]) for p in receipt["profiles"]]
    if len(profiles) != 2 or [p.slot for p in profiles] != [0,1] or profiles[0].resource_id == profiles[1].resource_id: raise ValueError("VIP resource inventory differs")
    retained = receipt["retained_sha256"]
    if not 40 <= len(retained) <= 1000: raise ValueError("VIP retained inventory budget")
    for name,checksum in retained.items():
        member = contained(root,name)
        if not any(member.is_relative_to(p.base) for p in profiles) and not member.is_relative_to(path.parent): raise ValueError("VIP retained evidence outside this run")
        if member.stat().st_size > 16*1024*1024 or hashlib.sha256(member.read_bytes()).hexdigest() != checksum: raise ValueError("VIP retained bytes changed")
        track_input(member,root,checksum)
    for m in receipt['measurements']:
        if type(m['index']) is not int or m['index'] not in (0,1) or m['result']['profile']!=profiles[m['index']].record():raise ValueError('VIP measurement index rebound')
    outcomes = [audit_measurement(root,m["result"]) for m in receipt["measurements"]]
    if len(outcomes) != 11 or outcomes.count((True,False)) != 4 or outcomes.count((False,False)) != 3 or outcomes.count((True,True)) != 4:
        raise ValueError("VIP positive/negative measurement inventory differs")
    for p,record in zip(profiles,receipt["profiles"]):
        p.validate_record(record);require_dependents_closed(p);track_absence(p.data/"postmaster.pid",root)
        schemas = read_json(p.base/"vip-components-run/tool_schemas.json",root)
        if {(s["server"],s["name"]) for s in schemas} != {(k,n) for k,names in METHODS.items() for n in names} or len(schemas) != 10:
            raise ValueError("VIP original schema inventory differs")
        journal = read_json(p.base/"vip-components-run/requests.json",root,limit=16*1024*1024)
        if journal["profile"] != p.record(): raise ValueError("VIP request journal belongs to another profile")
        pages = []; unknown = []
        for identity,e in journal["entries"].items():
            if str(UUID(identity))!=identity:raise ValueError('VIP request UUID differs')
            validate_call(e["server"],e["name"],e["arguments"])
            if e["fingerprint"] != fingerprint(e["server"]+"/"+e["name"],e["arguments"]): raise ValueError("VIP request fingerprint changed")
            for phase in ("before","after"):
                expected_ref=(p.base/"vip-components-run"/(identity+"."+phase+".json")).relative_to(root).as_posix()
                if e.get(phase+"_ref") != expected_ref or expected_ref not in retained: raise ValueError("VIP request snapshot rebound")
            left=load_snapshot(root,contained(root,e['before_ref']),p.base/'vip-components-run/initial_snapshot.json')
            right=load_snapshot(root,contained(root,e['after_ref']),p.base/'vip-components-run/initial_snapshot.json')
            if left['office']['readonly_sha256']!=right['office']['readonly_sha256']:raise ValueError('VIP read-only source changed across original operation')
            if e["state"] == "unknown": unknown.append(e)
            elif e["state"] != "committed" or e["response"].get("isError"): raise ValueError("VIP journal unresolved or fabricated success")
            elif e['server']=='email':
                mail = read_json(p.base/'email-components-run/requests.json',root)['entries'][identity]
                if (mail['state'] != 'committed' or mail['name'] != e['name'] or mail['arguments'] != e['arguments']
                    or mail['response'] != e['response'] or mail['fingerprint'] != fingerprint(e['name'],e['arguments'])):
                    raise ValueError('VIP nested mail request differs from outer intent')
                for phase, expected in (('before',left),('after',right)):
                    ref=(p.base/'email-components-run'/(identity+'.'+phase+'.json')).relative_to(root).as_posix()
                    if mail.get(phase+'_ref') != ref or ref not in retained:
                        raise ValueError('VIP nested mail snapshot rebound')
                    if load_snapshot(root,contained(root,ref),p.base/'vip-components-run/initial_snapshot.json') != expected:
                        raise ValueError('VIP nested and outer mail snapshots differ')
                verify_effect(e['name'],e['arguments'],e['response'],left,right)
            elif e['server']=='woocommerce' or e['name'] in ('list_sheets','get_sheet_data'):
                if left!=right:raise ValueError('VIP original read modified data')
            elif left['email']!=right['email']:raise ValueError('VIP cloud request modified mail')
            if e["server"] == "woocommerce":
                from integrations.vip_email_components import decode
                pages.append((e["arguments"]["page"],decode(e["response"])))
        if [n for n,rows in pages] != list(range(1,len(pages)+1)) or not pages or len(pages[-1][1]) >= 10 or any(len(r)!=10 for _,r in pages[:-1]):
            raise ValueError("VIP pagination termination differs")
        original=read_json(p.base/"vip-components-run/original_customers.json",root)
        if original["customers"] != [r for _,rows in pages for r in rows]: raise ValueError("VIP original MCP inventory differs")
        data=read_json(p.base/"vip-components-run/initial_snapshot.json",root,limit=16*1024*1024);check_customers(original["customers"],data)
        if data['office']['cells'] or any(data['email'].values()):raise ValueError('VIP resource did not initialize empty')
        if len(unknown) != p.slot or unknown and (unknown[0]["server"] != "google_sheet" or unknown[0]["name"] != "update_cells"):
            raise ValueError("VIP unknown fault fixture differs")
        if unknown and (not unknown[0]['response'].get('isError') or list(journal['entries'].values())[-1]!=unknown[0]):raise ValueError('VIP original fault or blocked replays differ')
        dependents=read_json(p.base/'dependent_processes.json',root)
        for server,filename in (('woocommerce','woocommerce-process.json'),('google_sheet','sheets-process.json')):
            if read_json(p.base/'vip-components-run'/filename,root)['pid']!=dependents['children'][server]['before']['pid']:raise ValueError('VIP child PID rebound')
        if read_json(p.base/'email-components-run/email-process.json',root)['pid']!=dependents['children']['email']['before']['pid']:raise ValueError('VIP mail PID rebound')
    if receipt["shared_before"] != receipt["shared_after"]: raise ValueError("VIP diagnostic changed shared data")
    for name in ("resource-sessions.json","isolated-sessions.json","synthetic-sessions.json"):
        before=path.parent/("ledger-before-"+name);after=path.parent/("ledger-after-"+name)
        if before.read_bytes()!=after.read_bytes(): raise ValueError("VIP diagnostic changed NeMo ledger")
    return {"checks":len(CHECKS),"measurements_recomputed":len(outcomes),"runtime_profiles_added":0,"official_benchmark_score_available":False,"external_messages_sent":0}
