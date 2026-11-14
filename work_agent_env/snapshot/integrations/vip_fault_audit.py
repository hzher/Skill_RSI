"""Retained evidence of an original PG email interrupted after message commit."""
import hashlib

from integrations.mainline_audit import read_json, source_binding, track_input, track_absence
from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.runtime_identity import expected_identity
from integrations.vip_email_components import load_snapshot
from integrations.vip_email_resource import request_identity, TASK_ID
from integrations.task_routing import TaskRouter
from workenv.core import contained

CHECKS=('three_attested_original_children','message_committed_while_log_waits','both_request_intents_prepared',
    'exact_worker_terminated','inflight_http_unknown','pg_recovery_blocked_before_reconciliation',
    'three_children_confirmed_exited','metadata_only_reconciliation','unknown_requests_never_replayed',
    'prepared_intents_and_message_preserved','old_identity_closed_and_dead','new_identity_has_independent_empty_state',
    'shared_data_and_resource_groups_unchanged')
EXTRA_SOURCES=('scripts/verify_nemo_vip_worker_fault.py','integrations/vip_fault_audit.py',
    'integrations/dependent_reconciliation.py','workenv/attested_process.py','workenv/process_identity.py')


def audit_fault(root,path):
    r=read_json(path,root)
    if (r.get('schema_version')!='nemo-vip-inflight-worker-fault-v1' or r.get('passed') is not True
        or [c['name'] for c in r['checks']]!=list(CHECKS) or any(c['passed'] is not True for c in r['checks'])
        or r.get('cleanup_errors')!=[] or any(type(r.get(k)) is not int or r[k]!=0 for k in ('real_model_calls','training_updates','training_admissions','external_messages_sent'))
        or r.get('training_eligible') is not False or r['shared_before']!=r['shared_after']):raise ValueError('VIP inflight fault scope differs')
    sources={**expected_identity(root),**{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in EXTRA_SOURCES}}
    if r['source_sha256']!=sources:raise ValueError('VIP inflight fault source inventory differs')
    source_binding(root,sources)
    profile=EpisodeProfile(root,r['profile']['resource_id'],r['profile']['slot']);profile.validate_record(r['profile'])
    proof=read_json(path.parent/'observations.json',root)
    fresh_profile=EpisodeProfile(root,r['fresh_resource_id'],proof['fresh_profile']['slot']);fresh_profile.validate_record(proof['fresh_profile'])
    if r['seed']['task_id']['task_id']!=TASK_ID or r['seed']['resources_session_id']!=profile.resource_id:raise ValueError('VIP fault target rebound')
    if not 12<=len(r['retained_sha256'])<=256:raise ValueError('VIP fault retained budget')
    for name,sha in r['retained_sha256'].items():
        member=contained(root,name)
        if not member.is_relative_to(profile.base) and not member.is_relative_to(fresh_profile.base) and not member.is_relative_to(path.parent):raise ValueError('VIP fault evidence escaped owned target')
        if member.stat().st_size>16*1024*1024 or hashlib.sha256(member.read_bytes()).hexdigest()!=sha:raise ValueError('VIP fault retained bytes changed')
        track_input(member,root,sha)
    worker=proof['worker_before'];deps=read_json(path.parent/'before-dependents.json',root)
    lease=read_json(path.parent/'before-lease.json',root)
    if (lease['workspace_owner']!=str(root) or lease['process']!=worker or worker['status']!='alive' or not worker.get('creation_id')
        or deps['profile']!=profile.record() or set(deps['children'])!={'woocommerce','google_sheet','email'}
        or proof['worker_terminated'] is not True or proof['worker_after']['status'] not in ('not_found','exited')):
        raise ValueError('VIP worker or child ownership proof differs')
    for server,child in deps['children'].items():
        if (child['profile']!=profile.record() or not child['before'].get('creation_id')
            or proof['children_after'][server]['pid']!=child['before']['pid'] or proof['children_after'][server]['status'] not in ('not_found','exited')):
            raise ValueError('VIP original child exit proof differs')
    source=profile.base/'vip-components-run/initial_snapshot.json'
    pending=load_snapshot(root,path.parent/'pending.json',source);frozen=load_snapshot(root,path.parent/'frozen.json',source)
    if pending!=frozen or len(frozen['email']['messages'])!=1 or frozen['email']['sent_log'] or frozen['office']['cells']:
        raise ValueError('VIP inflight partial state changed or was reset')
    message=frozen['email']['messages'][0];args=r['mutation']['arguments']
    if message['to_addr']!=[args['to']] or message['subject']!=args['subject'] or message['body_text']!=args['body']:
        raise ValueError('VIP partial message differs from original HTTP request')
    internal=request_identity(profile.resource_id,r['mutation']['request_id'])
    for folder in ('vip-components-run','email-components-run'):
        current=profile.base/folder/'requests.json';captured=path.parent/('prepared-'+folder+'.json')
        if current.read_bytes()!=captured.read_bytes():raise ValueError('VIP interrupted journal was rewritten')
        entry=read_json(current,root)['entries'][internal]
        if entry['state']!='prepared' or 'response' in entry or entry['arguments']!=args:raise ValueError('VIP inflight intent was fabricated as complete')
    if ((profile.base/'resources-session.json').read_bytes()!=(path.parent/'before-worker-ledger.json').read_bytes()
        or proof['reconciliation']['requests_replayed']!=0 or proof['reconciliation']['processes_terminated']!=0
        or proof['reconciliation']['slot_released'] is not False):raise ValueError('VIP fault recovery replayed or rewrote interrupted state')
    require_dependents_closed(profile);track_absence(profile.data/'postmaster.pid',root)
    if read_json(profile.base/'vip-task-session/task_profile.json',root)!=TaskRouter(root,'isolated').authorize(r['seed']):raise ValueError('VIP fault runtime profile differs')
    events=read_json(path.parent/'http_journal.json',root,limit=16*1024*1024)
    verifies=[e for e in events if e['route']=='/isolated/verify' and e['status']==200]
    old=[e['response']['result'] for e in verifies if e['request']['verification_input']['resources_session_id']==profile.resource_id]
    fresh=[e['response']['result'] for e in verifies if e['request']['verification_input']['resources_session_id']==r['fresh_resource_id']]
    if len(old)!=2 or any(v['reward'] is not None or v['mask_sample'] is not True for v in old) or len(fresh)!=1 or fresh[0]['reward']!=0 or fresh[0]['mask_sample'] is not False:
        raise ValueError('VIP inflight uncertainty became a valid reward')
    from integrations.vip_email_audit import audit_measurement
    from integrations.vip_email_resource import episode_result
    evaluated=fresh[0]['evaluation'];component=read_json(contained(root,evaluated['component_evaluation_ref']),root)
    if (component['profile']!=fresh_profile.record() or audit_measurement(root,component)!=(False,False)
        or episode_result(component)!=evaluated or read_json(contained(root,fresh[0]['evaluation_ref']),root)!=evaluated):
        raise ValueError('VIP fresh resource evaluation lacks independent original evidence')
    require_dependents_closed(fresh_profile);track_absence(fresh_profile.data/'postmaster.pid',root)
    attempts=[e for e in events if e['route']=='/isolated/tool']
    if len(attempts)!=3 or any(e['status']!=503 for e in attempts):raise ValueError('VIP inflight tool was replayed as success')
    if r['fresh_resource_id']==profile.resource_id or proof['gateway_closed_row']['state']!='closed':raise ValueError('VIP old identity reactivated')
    return {'checks':len(CHECKS),'partial_messages_retained':1,'requests_replayed':0,'inflight_worker_fault_verified':True}
