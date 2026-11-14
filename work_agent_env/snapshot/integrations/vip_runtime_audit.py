"""Real NeMo cloud/mail evidence; original input and HTTP rewards are recomputed."""
import json

from adapters.vip_email_evaluation import supplement
from integrations.vip_email_audit import audit_measurement
from integrations.vip_email_components import METHODS, load_snapshot, check_customers, decode
from integrations.vip_email_resource import episode_result, REWARD_VERSION, REFERENCE_MODE, request_identity, TASK_ID
from integrations.vip_resource_audit import audit_closed_state
from integrations.mainline_audit import read_json, track_input
from integrations.vip_seed import validated_seed, SEED_REF
from workenv.core import contained

CHECKS=('shared_backend_refused','two_owned_nemo_resources','ten_original_methods_no_artifacts_and_alias',
    'cookie_and_method_binding','empty_results_known_failure','original_customer_pagination_complete',
    'two_business_results_pass','outer_request_cache_and_conflict','foreign_cloud_refused',
    'cent_error_blind_spot_isolated','missing_log_blind_spot_rejected','wrong_recipient_rejected',
    'foreign_marker_masked_unknown','closing_first_preserves_second','meal_reuses_released_slot',
    'partial_email_unknown_retains_message','unknown_blocks_replay_and_measurement',
    'three_children_closed_and_tombstones_enforced','shared_data_and_other_ledgers_unchanged')
EXTRA_SOURCES=('scripts/verify_nemo_vip.py','integrations/vip_runtime_audit.py','integrations/vip_email_audit.py',
    'integrations/vip_resource_audit.py','integrations/office_runtime_audit.py','scripts/verify_calendar_isolation.py')


def checked_measurement(root,component):
    original,supplemental=audit_measurement(root,component)
    # Use the profile-bound source, never infer it from a foreign measurement path.
    p=component['profile'];source=root/'.runtime/native-episodes'/p['resource_id']/'vip-components-run/initial_snapshot.json'
    data=load_snapshot(root,contained(root,component['snapshot_ref']),source)
    return {'component':component['component_measurement_passed'],'original':original,
        'supplemental':component['supplemental'],'data':{**data,'readonly_sha256':data['office']['readonly_sha256']}}


def audit_vip(root,path):
    from integrations.office_runtime_audit import audit_report
    receipt=read_json(path,root);finals={}
    for m in receipt.get('measurements',[]):
        if m['index']>1 or m['result']['mask_sample']:continue
        c=read_json(contained(root,m['result']['evaluation']['component_evaluation_ref']),root)
        source=root/'.runtime/native-episodes'/c['profile']['resource_id']/'vip-components-run/initial_snapshot.json'
        finals[c['profile']['resource_id']]=load_snapshot(root,contained(root,c['snapshot_ref']),source)
    def inventory(root,profile,data):
        seed,sql=validated_seed(root,read_json(root/SEED_REF,root))
        track_input(sql,root)
        if data['office']['readonly_sha256']!=seed['readonly_sha256']:raise ValueError('VIP runtime readonly seed differs')
        run=profile.base/'vip-task-session';base=profile.base/'vip-components-run';source=base/'initial_snapshot.json'
        schemas=read_json(base/'tool_schemas.json',root)
        if len(schemas)!=10 or {(s['server'],s['name']) for s in schemas}!={(s,n) for s,names in METHODS.items() for n in names}:
            raise ValueError('VIP runtime original inventory differs')
        bindings=read_json(run/'request_bindings.json',root)
        journal=read_json(base/'requests.json',root,limit=16*1024*1024)
        if bindings['profile']!=profile.record() or journal['profile']!=profile.record():raise ValueError('VIP runtime request owner differs')
        ids={}
        for key,e in bindings['entries'].items():
            internal=request_identity(profile.resource_id,e['external_request_id'],purpose=e['purpose'])
            if key!=e['purpose']+'/'+e['external_request_id'] or internal!=e['internal_request_id']:raise ValueError('VIP runtime UUID binding differs')
            ids[internal]=e
        original=read_json(base/'original_customers.json',root);check_customers(original['customers'],data)
        pages=[];unknown=[]
        for identity,e in journal['entries'].items():
            if identity not in ids:raise ValueError('VIP original RPC lacks request binding')
            if e['server']=='woocommerce' and ids[identity]['purpose']=='source':pages.append((e['arguments']['page'],decode(e['response'])))
            if e['state']=='unknown':unknown.append((identity,e))
        if ([n for n,rows in pages]!=list(range(1,len(pages)+1)) or not pages or len(pages[-1][1])>=10
            or [r for n,rows in pages for r in rows]!=original['customers']):raise ValueError('VIP original source pagination differs')
        expected=finals[profile.resource_id]
        if unknown:
            if len(unknown)!=1 or unknown[0][1]['server']!='email':raise ValueError('VIP runtime unknown fixture differs')
            nested=read_json(profile.base/'email-components-run/requests.json',root)['entries'][unknown[0][0]]
            if nested['state']!='unknown':raise ValueError('VIP unknown mail intent lost')
            expected=load_snapshot(root,contained(root,nested['after_ref']),source)
            if len(expected['email']['messages'])!=6 or len(expected['email']['sent_log'])!=5:raise ValueError('VIP partial mail not retained')
        closed=read_json(run/'closed.json',root)
        final=load_snapshot(root,run/'closed_snapshot.json',source)
        audit_closed_state(profile,closed,final,expected)
        if closed['outcome_unknown'] is not bool(unknown):raise ValueError('VIP close lost unknown status')
        events=read_json(path.parent/'http_journal.json',root,limit=16*1024*1024)
        for e in events:
            if e['route']!='/isolated/tool' or e['status']!=200 or e['request']['resources_session_id']!=profile.resource_id:continue
            b=e['request'];internal=request_identity(profile.resource_id,b['request_id']);call=journal['entries'][internal]
            if (call['server']!=b['server'] or call['name']!=b['name'] or call['arguments']!=b['arguments']
                or call['response']!=e['response']['result']):raise ValueError('VIP raw HTTP tool differs from durable original RPC')
    return audit_report(root,path,family='vip',task_id=TASK_ID,checks=CHECKS,extra_sources=EXTRA_SOURCES,
        episode_result=episode_result,audit_measurement=checked_measurement,check_inventory=inventory,
        reward_version=REWARD_VERSION,reference_mode=REFERENCE_MODE)


def audit_lifecycle(root,path):
    from integrations.office_runtime_audit import audit_lifecycle as common
    return common(root,path,family='vip',task_id=TASK_ID,expected_children={'woocommerce','google_sheet','email'})
