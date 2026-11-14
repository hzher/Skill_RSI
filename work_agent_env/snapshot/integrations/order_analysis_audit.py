"""Recompute the third task's retained original and supplemental evidence."""
import hashlib
import json
from collections import Counter

from adapters.order_analysis_evaluation import classify_original, original_checks
from adapters.order_analysis_validation import contract_identity, measure_workbook, EVALUATION_SHA256
from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_input, track_absence
from integrations.runtime_identity import expected_identity
from workenv.core import contained

CHECKS = ('shared_order_task_rejected','two_order_episodes','original_order_mcp_schemas','cookie_and_write_allowlist',
          'two_known_empty_failures','complete_original_pagination','two_original_and_supplemental_success',
          'bounded_workbook_roundtrip','supplement_rejects_original_tolerance','write_replay_and_restored_success',
          'infrastructure_unknown_and_restore','closing_one_preserves_other','cross_task_slot_reuse',
          'closed_identity_and_unknown','children_and_pg_stopped','shared_data_and_other_ledgers_unchanged')
EXTRA_SOURCES=('scripts/verify_nemo_order_analysis.py','integrations/order_analysis_audit.py','scripts/prepare_episode_seed.py',
               'integrations/dependent_reconciliation.py','scripts/verify_calendar_isolation.py')


def audit_order(root,path):
    receipt=read_json(path,root)
    if (receipt.get('schema_version')!='nemo-original-order-analysis-diagnostic-v1' or receipt.get('passed') is not True
        or receipt.get('execution_kind')!='scripted_original_task_diagnostic' or receipt.get('cleanup_errors')!=[]
        or [c['name'] for c in receipt.get('checks',[])]!=list(CHECKS) or any(c['passed'] is not True for c in receipt['checks'])
        or any(receipt.get(k) is not False for k in ('training_eligible','reference_solutions_read','verified_task_bodies_read'))
        or any(type(receipt.get(k)) is not int or receipt[k]!=0 for k in ('real_model_calls','training_updates','training_admissions'))):
        raise ValueError('Order diagnostic scope or checks differ')
    expected={**expected_identity(root),**{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in EXTRA_SOURCES}}
    if receipt['source_sha256']!=expected:raise ValueError('Order diagnostic sources differ')
    source_binding(root,expected)
    retained=receipt['retained_sha256']
    if not isinstance(retained,dict) or not 20<=len(retained)<=512:raise ValueError('Order retained inventory differs')
    for name,digest in retained.items():
        member=contained(root,name)
        if member.stat().st_size>16*1024*1024 or hashlib.sha256(member.read_bytes()).hexdigest()!=digest:raise ValueError('Order retained bytes differ')
        track_input(member,root,digest)
    journal=read_json(path.parent/'http_journal.json',root,limit=16*1024*1024)
    by_sid={s['resources_session_id']:i for i,s in enumerate(receipt['seeds'])}
    actual=[]
    for event in journal:
        if event.get('route')!='/isolated/verify' or event.get('status')!=200:continue
        request,reply=event['request'],event['response']
        sid=request['verification_input']['resources_session_id']
        if (sid not in by_sid or reply['episode_id']!=request['episode_id'] or reply['task_id']!=request['task_id']
            or request['episode_id']!=receipt['seeds'][by_sid[sid]]['episode_id']):
            raise ValueError('Order HTTP verification identity differs')
        actual.append({'index':by_sid[sid],'result':reply['result']})
    encoded=lambda value:json.dumps(value,sort_keys=True,separators=(',',':'))
    if Counter(map(encoded,actual))!=Counter(map(encoded,receipt['measurements'])):
        raise ValueError('Order measurement summary differs from raw HTTP replies')
    evaluations=0;blind_spot=False
    for row in receipt['measurements']:
        result=row['result']
        if row['index']>1:continue
        if result['mask_sample']:
            if result['reward'] is not None:raise ValueError('Unknown order result acquired a reward')
            continue
        ref=result['evaluation_ref']
        if ref not in retained:raise ValueError('Unbound order evaluation')
        measured=read_json(contained(root,ref),root)
        if measured!=result['evaluation']:raise ValueError('Order HTTP measurement differs from retained evaluator')
        directory=contained(root,ref).parent
        if hashlib.sha256((directory/'original_evaluation.py').read_bytes()).hexdigest()!=EVALUATION_SHA256:
            raise ValueError('Order evaluator is not the pinned original')
        raw=(directory/'original.stdout.txt').read_text()
        err=(directory/'original.stderr.txt').read_text()
        upstream=classify_original(measured['upstream_exit_code'],raw,err)
        if measured.get('upstream_checks')!=(original_checks(raw) if type(upstream) is bool else []):
            raise ValueError('Order original check categories differ from retained output')
        data=read_json(contained(root,measured['source_snapshot_ref']),root)
        profile=EpisodeProfile(root,receipt['seeds'][row['index']]['resources_session_id'],receipt['profiles'][str(row['index'])]['slot'])
        # Replay only the deterministic measurement over a retained workbook version.
        # Each write's immutable history supplies the exact bytes at evaluation.
        workbook_hash=measured.get('workbook_sha256')
        workbook=directory/'Order_Analysis.xlsx'
        if workbook_hash:
            if workbook.relative_to(root).as_posix() not in retained or hashlib.sha256(workbook.read_bytes()).hexdigest()!=workbook_hash:raise ValueError('Order workbook evidence differs')
        else:
            if measured.get('workbook_missing') is not True: raise ValueError('Unbound missing workbook state')
            track_absence(workbook,root)
        supplemental=measure_workbook(data['orders'],data['products'],data['events'],directory,source_binding=contract_identity())
        if upstream!=measured['upstream_passed'] or supplemental.get('supplemental_passed')!=measured['supplemental_passed']:
            raise ValueError('Order original/supplemental recomputation differs')
        if float(upstream and supplemental['supplemental_passed'])!=result['reward']:raise ValueError('Order reward differs')
        blind_spot |= upstream is True and supplemental['supplemental_passed'] is False
        evaluations+=1
    if evaluations<6 or not blind_spot or receipt['shared_before']!=receipt['shared_after']:raise ValueError('Incomplete original order evidence')
    for record in receipt['profiles'].values():
        profile=EpisodeProfile(root,record['resource_id'],record['slot']);profile.validate_record(record)
        track_absence(profile.data/'postmaster.pid',root);require_dependents_closed(profile)
    return {'passed':True,'original_task_id':'wc-order-analysis-gcal','checks':len(CHECKS),'evaluations_recomputed':evaluations,
            'training_admissions':0,'original_tolerance_blind_spot_rejected':True}


def audit_lifecycle(root,path):
    from integrations.isolated_audit import audit_isolated, CHECKS as ISOLATED_CHECKS
    from integrations.task_routing import TaskRouter
    mapping={'nemo-order-recovery-diagnostic-v1':ISOLATED_CHECKS['nemo-isolated-recovery-diagnostic-v1'],
             'nemo-order-expiry-diagnostic-v1':ISOLATED_CHECKS['nemo-isolated-expiry-diagnostic-v1']}
    receipt=read_json(path,root)
    if receipt.get('task_id')!='wc-order-analysis-gcal' or any(receipt.get(k) is not False for k in ('training_eligible','reference_solutions_read','verified_task_bodies_read')):
        raise ValueError('Order lifecycle task/scope differs')
    result=audit_isolated(root,path,expected_task='wc-order-analysis-gcal',checks_by_schema=mapping)
    journal=read_json(path.parent/'http_journal.json',root)
    seeded=[e['request'] for e in journal if e['route']=='/isolated/seed_session' and e['status']==200]
    if len(seeded)!=(2 if 'recovery' in receipt['schema_version'] else 1):raise ValueError('Order lifecycle seed count differs')
    for seed in seeded:
        base=root/'.runtime/native-episodes'/seed['resources_session_id']
        owner=read_json(base/'cluster_owner.json',root)
        profile=EpisodeProfile(root,seed['resources_session_id'],owner['slot']);profile.validate_record(owner)
        if read_json(base/'order-task-session/task_profile.json',root)!=TaskRouter(root,'isolated').authorize(seed):
            raise ValueError('Order lifecycle profile differs')
        require_dependents_closed(profile)
        dependents=read_json(base/'dependent_processes.json',root)
        if set(dependents['children'])!={'woocommerce','google_calendar'}:raise ValueError('Order lifecycle dependency inventory differs')
        track_absence(profile.data/'postmaster.pid',root)
        if read_json(base/'resources-session.json',root)['sessions'][profile.resource_id]['state']!='closed':
            raise ValueError('Order lifecycle worker did not close')
    if 'expiry' in receipt['schema_version'] and receipt.get('normal_lease_restored_seconds')!=900:raise ValueError('Order expiry did not restore normal lease')
    return {**result,'original_order_episodes':len(seeded)}
