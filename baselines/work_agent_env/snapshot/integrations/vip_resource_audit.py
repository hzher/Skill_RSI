"""Audit direct cloud/mail adapter preparation, without claiming NeMo execution."""
import hashlib
from uuid import UUID

from integrations.mainline_audit import read_json, source_binding, track_input, track_absence
from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.vip_email_audit import audit_measurement, SOURCES as COMPONENT_SOURCES
from integrations.vip_email_components import METHODS, load_snapshot, validate_call, decode, check_customers
from integrations.vip_email_resource import episode_result, request_identity, TASK_ID
from integrations.email_components import fingerprint, verify_effect
from workenv.core import contained, ResourceError

CHECKS = ("no_nemo_execution_or_training_admission", "two_owned_empty_resources", "ten_methods_without_artifact_surface",
    "workbook_operations_refused", "original_source_pagination_and_empty_failure", "complete_business_diagnostic_pass",
    "stable_request_cache_and_conflict_refusal", "foreign_cloud_identity_refused", "cent_error_blind_spot_diagnostic_failure",
    "restored_business_diagnostic_pass", "closing_first_preserves_second", "partial_mail_write_retained_unknown",
    "unknown_blocks_old_new_and_measurement", "unknown_close_preserves_data_without_reset",
    "all_three_original_children_closed", "owned_postgres_closed", "shared_database_and_nemo_ledgers_unchanged")
SOURCES = tuple(dict.fromkeys((*COMPONENT_SOURCES, "integrations/vip_email_resource.py",
    "integrations/vip_resource_audit.py", "scripts/verify_vip_resource_adapter.py")))


def audit_closed_state(profile, record, final, expected):
    ref=(profile.base/'vip-task-session/closed_snapshot.json').relative_to(profile.owner).as_posix()
    if (record.get('profile')!=profile.record() or record.get('reset_performed') is not False
        or record.get('dependent_processes_closed') is not True or record.get('snapshot_ref')!=ref
        or type(record.get('external_messages_sent')) is not int or record['external_messages_sent']!=0):
        raise ValueError('VIP close owner, process proof or snapshot binding differs')
    if expected is None or final!=expected:
        raise ValueError('VIP close state differs from last observed original input or operation')


def audit_preparation(root, path):
    receipt = read_json(path, root)
    if (receipt.get('schema_version') != 'vip-resource-adapter-preparation-v1' or receipt.get('passed') is not True
        or [c['name'] for c in receipt['checks']] != list(CHECKS) or any(c['passed'] is not True for c in receipt['checks'])
        or any(receipt.get(k) is not False for k in ('runtime_task_activated','nemo_task_executed'))
        or receipt.get('task_passed') is not None or receipt.get('reward') is not None
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ('external_messages_sent','real_model_calls','training_updates','training_admissions'))
        or set(receipt.get('source_sha256',{})) != set(SOURCES)):
        raise ValueError('VIP adapter preparation scope or checks differ')
    source_binding(root,receipt['source_sha256'])
    profiles=[EpisodeProfile(root,p['resource_id'],p['slot']) for p in receipt['profiles']]
    if len(profiles)!=2 or [p.slot for p in profiles]!=[0,1] or profiles[0].resource_id==profiles[1].resource_id:
        raise ValueError('VIP adapter resource inventory differs')
    retained=receipt['retained_sha256']
    if not 40 <= len(retained) <= 1000:raise ValueError('VIP adapter retained inventory budget')
    for name,checksum in retained.items():
        member=contained(root,name)
        if not any(member.is_relative_to(p.base) for p in profiles) and not member.is_relative_to(path.parent):
            raise ValueError('VIP adapter evidence escaped resource')
        if member.stat().st_size>16*1024*1024 or hashlib.sha256(member.read_bytes()).hexdigest()!=checksum:
            raise ValueError('VIP adapter retained bytes changed')
        track_input(member,root,checksum)
    outcomes=[];last_measurement={}
    for entry in receipt['measurements']:
        index=entry['index'];result=entry['result']
        if type(index) is not int or index not in (0,1):raise ValueError('VIP adapter measurement index differs')
        component=read_json(contained(root,result['component_evaluation_ref']),root)
        if component['profile']!=profiles[index].record():raise ValueError('VIP adapter measurement rebound')
        outcomes.append(audit_measurement(root,component))
        profile=profiles[index]
        last_measurement[index]=load_snapshot(root,contained(root,component['snapshot_ref']),profile.base/'vip-components-run/initial_snapshot.json')
        if episode_result(component)!=result or read_json(contained(root,entry['evaluation_ref']),root)!=result:
            raise ValueError('VIP diagnostic reward classification or version differs')
    if outcomes!=[(False,False),(False,False),(True,True),(True,False),(True,True)]:
        raise ValueError('VIP adapter positive and negative measurement inventory differs')
    for p,record in zip(profiles,receipt['profiles']):
        p.validate_record(record);require_dependents_closed(p);track_absence(p.data/'postmaster.pid',root)
        base=p.base/'vip-components-run';run=p.base/'vip-task-session';source=base/'initial_snapshot.json'
        initial=read_json(source,root,limit=16*1024*1024)
        if any(initial['email'].values()) or initial['office']['cells']:raise ValueError('VIP adapter seed not empty')
        schemas=read_json(base/'tool_schemas.json',root)
        if len(schemas)!=10 or {(s['server'],s['name']) for s in schemas}!={(s,n) for s,names in METHODS.items() for n in names}:
            raise ValueError('VIP adapter method inventory differs')
        bindings=read_json(run/'request_bindings.json',root)
        if bindings['profile']!=p.record():raise ValueError('VIP adapter request owner differs')
        mapping={}
        for key,e in bindings['entries'].items():
            if key!=e['purpose']+'/'+e['external_request_id'] or e['internal_request_id']!=request_identity(p.resource_id,e['external_request_id'],purpose=e['purpose']):
                raise ValueError('VIP outer and inner request binding differs')
            mapping[e['internal_request_id']]=e
        journal=read_json(base/'requests.json',root,limit=16*1024*1024)
        mail=read_json(p.base/'email-components-run/requests.json',root,limit=16*1024*1024)
        if journal['profile']!=p.record() or mail['profile']!=p.record():raise ValueError('VIP adapter journal rebound')
        pages=[];unknown=[];unknown_after=None
        for identity,e in journal['entries'].items():
            if str(UUID(identity))!=identity or identity not in mapping:raise ValueError('VIP request lacks external binding')
            validate_call(e['server'],e['name'],e['arguments'])
            if e['fingerprint']!=fingerprint(e['server']+'/'+e['name'],e['arguments']):raise ValueError('VIP request fingerprint differs')
            states={}
            for phase in ('before','after'):
                ref=(base/(identity+'.'+phase+'.json')).relative_to(root).as_posix()
                if phase=='after' and e['state']=='unknown' and 'after_ref' not in e:continue
                if e.get(phase+'_ref')!=ref or ref not in retained:raise ValueError('VIP request snapshot rebound')
                states[phase]=load_snapshot(root,contained(root,ref),source)
            if e['state']=='unknown':unknown.append(identity)
            elif e['state']!='committed' or e['response'].get('isError'):raise ValueError('VIP request state invalid')
            if e['server']=='email':
                nested=mail['entries'][identity]
                if (nested['name']!=e['name'] or nested['arguments']!=e['arguments'] or nested['state']!=e['state']
                    or nested['fingerprint']!=fingerprint(e['name'],e['arguments'])):raise ValueError('VIP nested mail intent differs')
                pair=[]
                for phase in ('before','after'):
                    ref=(p.base/'email-components-run'/(identity+'.'+phase+'.json')).relative_to(root).as_posix()
                    if nested.get(phase+'_ref')!=ref or ref not in retained:raise ValueError('VIP mail snapshot rebound')
                    data=load_snapshot(root,contained(root,ref),source);pair.append(data)
                    if phase in states and data!=states[phase]:raise ValueError('VIP nested mail state differs')
                if e['state']=='committed':
                    if nested['response']!=e['response']:raise ValueError('VIP mail response differs')
                    verify_effect(e['name'],e['arguments'],nested['response'],*pair)
                else:
                    unknown_after=pair[1]
                    try:verify_effect(e['name'],e['arguments'],nested['response'],*pair)
                    except ResourceError:pass
                    else:raise ValueError('VIP partial email unexpectedly complete')
                    if len(pair[1]['email']['messages'])!=1 or pair[1]['email']['sent_log']:
                        raise ValueError('VIP partial mail fixture differs')
            elif e['state']=='committed':
                if e['server']=='woocommerce' or e['name'] in ('list_sheets','get_sheet_data'):
                    if states['before']!=states['after']:raise ValueError('VIP original read modified data')
                elif states['before']['email']!=states['after']['email']:raise ValueError('VIP cloud operation changed mail')
            if e['server']=='woocommerce':
                if mapping[identity]['purpose']!='source':raise ValueError('VIP source pagination namespace differs')
                pages.append((e['arguments']['page'],decode(e['response'])))
        original=read_json(base/'original_customers.json',root)
        if ([n for n,rows in pages]!=list(range(1,len(pages)+1)) or not pages or len(pages[-1][1])>=10
            or any(len(rows)!=10 for _,rows in pages[:-1]) or [r for _,rows in pages for r in rows]!=original['customers']):
            raise ValueError('VIP original source pagination differs')
        check_customers(original['customers'],initial)
        if len(unknown)!=p.slot or set(mail['entries'])!={k for k,e in journal['entries'].items() if e['server']=='email'}:
            raise ValueError('VIP unknown or nested mail inventory differs')
        closed=read_json(run/'closed.json',root)
        if closed['outcome_unknown'] is not bool(p.slot):raise ValueError('VIP close outcome differs')
        final=load_snapshot(root,contained(root,closed['snapshot_ref']),source)
        audit_closed_state(p,closed,final,unknown_after if p.slot else last_measurement[p.slot])
        if len(final['email']['messages'])!=(5 if p.slot==0 else 1) or len(final['email']['sent_log'])!=(5 if p.slot==0 else 0):
            raise ValueError('VIP close cleared or duplicated mail')
        dependents=read_json(p.base/'dependent_processes.json',root)
        if set(dependents['children'])!=set(METHODS):raise ValueError('VIP close omitted a child')
        for server,directory,filename in (('woocommerce',base,'woocommerce-process.json'),('google_sheet',base,'sheets-process.json'),('email',p.base/'email-components-run','email-process.json')):
            if read_json(directory/filename,root)['pid']!=dependents['children'][server]['before']['pid']:
                raise ValueError('VIP original child PID rebound')
    if receipt['shared_before']!=receipt['shared_after']:raise ValueError('VIP adapter changed shared data')
    for name in ('resource-sessions.json','isolated-sessions.json','synthetic-sessions.json'):
        if (path.parent/('ledger-before-'+name)).read_bytes()!=(path.parent/('ledger-after-'+name)).read_bytes():
            raise ValueError('VIP adapter changed NeMo ledger')
    return {'checks':len(CHECKS),'measurements_recomputed':len(outcomes),'runtime_profiles_added':0,
        'nemo_task_executed':False,'external_messages_sent':0}
