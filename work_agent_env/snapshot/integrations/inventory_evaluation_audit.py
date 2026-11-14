"""Recompute SQL-fixture verifier evidence; never attest uncalled MCP tools."""
import hashlib
import json
from pathlib import Path

from adapters import inventory_alert_evaluation as evaluation
from integrations import inventory_data as data
from integrations.episode_cluster import EpisodeProfile,EpisodeCluster
from integrations.mainline_audit import read_json,source_binding,track_input,track_absence
from workenv.core import contained

CHECKS=('owned_sql_derived_reference_not_upstream_answer','complete_original_products_match_sql',
 'positive_original_and_supplemental','wrong_name_original_blindspot','wrong_time_zone_original_blindspot',
 'extra_calendar_original_blindspot','wrong_recipient_rejected','missing_send_log_original_blindspot',
 'restored_positive','closed_database_unknown','shared_database_and_ledgers_unchanged','owned_processes_and_pg_closed')
CASES=('positive','wrong_name','wrong_time_zone','extra_calendar','wrong_recipient','missing_send_log','restored_positive','closed_database')
SOURCES=('adapters/inventory_alert_evaluation.py','integrations/inventory_evaluation_audit.py','scripts/verify_inventory_evaluation.py',
 'adapters/inventory_alert_validation.py','adapters/inventory_alert_artifacts.py','integrations/inventory_data.py',
 'integrations/inventory_data_audit.py','integrations/episode_cluster.py','integrations/order_analysis_resource.py',
 'scripts/prepare_inventory_data_seed.py','training/inventory_task_review.py')


def audit_measurement(root,path,profile):
    record=read_json(path,root)
    if (record.get('schema_version')!='inventory-original-component-measurement-v1' or record.get('task_id')!=data.TASK_ID
        or record.get('profile')!=profile.record() or record.get('evaluation_ref')!=path.relative_to(root).as_posix()
        or record.get('reference_mode')!=evaluation.REFERENCE_MODE or record.get('task_passed') is not None or record.get('reward') is not None
        or any(record.get(k) is not False for k in ('upstream_reference_used','reference_files_read','official_benchmark_score_available',
            'nemo_task_executed','runtime_activation','training_eligible'))
        or any(type(record.get(k)) is not int or record[k]!=0 for k in ('real_model_calls','training_updates'))):
        raise ValueError('Inventory measurement acquired task/training authority or changed identity')
    if record.get('input_snapshot_ref') is None:
        if record.get('original_evaluator_executed') is not False or record.get('original_passed') is not None or record.get('component_measurement_passed') is not None or record.get('evaluation_status')!='error' or not record.get('error'):
            raise ValueError('Missing snapshot was not kept unknown')
        track_absence(path.parent/'original_evaluation.py',root)
        return record
    if record['input_snapshot_ref']!=(path.parent/'input_snapshots.json').relative_to(root).as_posix():
        raise ValueError('Inventory measurement snapshot path rebound')
    snapshots=read_json(contained(root,record['input_snapshot_ref']),root,limit=8*1024*1024)
    before,after=snapshots['before'],snapshots['after'];data.validate_snapshot(before)
    if before!=after:raise ValueError('Original evaluator did not preserve its SQL input')
    cluster=EpisodeCluster(profile)
    proof_path,proof=evaluation.source_proof(root,cluster,before)
    if record.get('source_proof_ref')!=proof_path.relative_to(root).as_posix() or record.get('source_completeness_verified') is not True or record.get('owned_database_snapshot_verified') is not True:
        raise ValueError('Original product proof identity or claims differ')
    read_json(proof_path,root);read_json(contained(root,proof['pages_ref']),root,limit=4*1024*1024)
    script=path.parent/'original_evaluation.py';raw,identity=data.public_blob(root,'evaluation/main.py')
    if script.read_bytes()!=raw or record['original_evaluator_source']!=identity or record['original_evaluator_sha256']!=hashlib.sha256(raw).hexdigest():
        raise ValueError('Original inventory evaluator source bytes differ')
    track_input(script,root)
    def bytes_member(member):
        p=path.parent/member
        if not p.is_file() or p.stat().st_size>256*1024:raise ValueError('Inventory verifier member missing or oversized')
        track_input(p,root);return p.read_bytes()
    oracle=bytes_member('oracle/'+evaluation.validation.FILENAME)
    if oracle!=evaluation.sql_report(before).encode('utf-8'):raise ValueError('Reference is not the owned SQL-derived report')
    report=bytes_member('agent/'+evaluation.validation.FILENAME).decode('utf-8')
    original=evaluation.classify_original(record['original_exit_code'],bytes_member('original.stdout.txt').decode('utf-8'),bytes_member('original.stderr.txt').decode('utf-8'))
    combined=evaluation.combine(original,evaluation.supplement(before,report))
    if any(record.get(k)!=v for k,v in combined.items()) or record.get('original_evaluator_executed') is not True:
        raise ValueError('Inventory original/supplemental result cannot be reproduced')
    return record


def audit(root,path):
    root=Path(root).resolve();receipt=read_json(path,root)
    if (receipt.get('schema_version')!='inventory-evaluator-components-v1' or receipt.get('passed') is not True
        or [c.get('name') for c in receipt.get('checks',[])]!=list(CHECKS) or any(c.get('passed') is not True for c in receipt['checks'])
        or receipt.get('fixture_mode')!='explicit_sql_not_original_calendar_or_email_tools'
        or any(receipt.get(k) is not False for k in ('nemo_task_executed','runtime_activation','training_admission','email_tools_called','calendar_tools_called','reference_files_read','official_benchmark_score_available'))):
        raise ValueError('Inventory verifier receipt scope differs')
    source_binding(root,receipt.get('source_sha256'))
    if set(receipt['source_sha256'])!=set(SOURCES):raise ValueError('Inventory verifier source coverage differs')
    retained=receipt.get('retained_sha256',{})
    if not 10<=len(retained)<=80:raise ValueError('Inventory verifier retained file budget')
    for name,expected in retained.items():
        if not name.startswith(('.runtime/native-episodes/','evidence/mainline/inventory-evaluation-')):raise ValueError('Inventory verifier evidence path outside scope')
        p=contained(root,name)
        if not p.is_file() or p.stat().st_size>8*1024*1024 or hashlib.sha256(p.read_bytes()).hexdigest()!=expected:
            raise ValueError('Inventory verifier retained bytes changed')
        track_input(p,root,expected)
    identity=receipt['profile'];profile=EpisodeProfile(root,identity['resource_id'],identity['slot']);profile.validate_record(identity)
    if read_json(profile.base/'cluster_owner.json',root)!=profile.record():raise ValueError('Inventory verifier PG ownership differs')
    if [c.get('name') for c in receipt['measurements']]!=list(CASES):raise ValueError('Inventory verifier case coverage differs')
    for item in receipt['measurements']:
        p=contained(root,item['evaluation_ref'])
        if p.parent.parent!=profile.base/'inventory-evaluations' or p.name!='result.json' or p.relative_to(root).as_posix() not in retained:
            raise ValueError('Inventory measurement path outside its retained owned evaluation')
        measured=read_json(p,root)
        if measured.get('input_snapshot_ref'):
            required=[p.parent/'input_snapshots.json',p.parent/'original_evaluation.py',p.parent/'original.stdout.txt',p.parent/'original.stderr.txt',
                p.parent/'agent'/evaluation.validation.FILENAME,p.parent/'oracle'/evaluation.validation.FILENAME,profile.base/evaluation.SOURCE_NAME]
            if any(member.relative_to(root).as_posix() not in retained for member in required):
                raise ValueError('Inventory measurement required evidence was not retained')
    cases={c['name']:audit_measurement(root,contained(root,c['evaluation_ref']),profile) for c in receipt['measurements']}
    for name in ('positive','restored_positive'):
        if cases[name]['component_measurement_passed'] is not True:raise ValueError('Inventory positive fixture did not pass')
    for name in ('wrong_name','wrong_time_zone','extra_calendar','missing_send_log'):
        if cases[name]['original_passed'] is not True or cases[name]['supplemental']['supplemental_passed'] is not False or cases[name]['component_measurement_passed'] is not False:
            raise ValueError('Inventory original blindspot not independently reproduced')
    if cases['wrong_recipient']['original_passed'] is not False or cases['wrong_recipient']['component_measurement_passed'] is not False:
        raise ValueError('Inventory wrong recipient not rejected')
    if cases['closed_database']['component_measurement_passed'] is not None:raise ValueError('Closed inventory PG was scored')
    states=[read_json(contained(root,c['input_snapshot_ref']),root,limit=8*1024*1024)['before'] for c in cases.values() if c['input_snapshot_ref']]
    if any(s['readonly_sha256']!=states[0]['readonly_sha256'] or s['products']!=states[0]['products'] for s in states):
        raise ValueError('Inventory source data changed across fixture cases')
    if receipt['shared_before']!=receipt['shared_after'] or receipt['ledgers_before']!=receipt['ledgers_after']:raise ValueError('Shared resources changed')
    child=read_json(contained(root,receipt['child_session_ref']),root)
    if child.get('profile')!=profile.record() or child.get('closed') is not True or child['before'].get('creation_id') is None or child['after'].get('pid')!=child['before'].get('pid') or not (child['after'].get('status') in ('exited','not_found') or child['after'].get('status')=='alive' and child['after'].get('creation_id') and child['after']['creation_id']!=child['before']['creation_id']):
        raise ValueError('Original product child closure unknown')
    track_absence(profile.data/'postmaster.pid',root)
    if receipt.get('pg_stopped') is not True:raise ValueError('Inventory PG did not close')
    return {'measurements_recomputed':len(cases),'original_evaluator_runs':7,'original_blindspots':4,
        'reference_mode':evaluation.REFERENCE_MODE,'original_calendar_email_tools_called':False,
        'new_runtime_profiles':0,'training_admission':False}
