"""Interrupt an attested worker while original mail waits after message commit."""
import copy
from datetime import datetime,timezone
import hashlib
import json
import sys
import threading
import time
from uuid import uuid4

from mainline_runtime import ROOT,VM,bridge_request
sys.path.insert(0,str(ROOT))
from integrations.nemo_http import NeMoHTTP
from integrations.episode_cluster import EpisodeProfile,EpisodeCluster,recover_orphan_cluster,require_dependents_closed
from integrations.dependent_reconciliation import reconcile_dependents
from integrations.vip_email_resource import TASK_ID,request_identity
from integrations.vip_email_components import save_snapshot
from integrations.email_components import snapshot
from integrations.vip_fault_audit import CHECKS,EXTRA_SOURCES,audit_fault
from integrations.runtime_identity import expected_identity
from scripts.verify_calendar_isolation import shared_snapshot
from scripts.verify_email_components import shared_email_digest
from workenv.core import ResourceError,save
from workenv.process_identity import process_identity
from workenv.attested_process import LinuxAttestedChild


def main():
    if sys.platform!='linux':raise ResourceError('Owned inflight worker probe requires Linux pidfd')
    output=ROOT/'evidence/mainline'/('nemo-vip-worker-fault-'+uuid4().hex);output.mkdir()
    client,caller,fresh_client=NeMoHTTP(),NeMoHTTP(),NeMoHTTP()
    sid,fresh=str(uuid4()),str(uuid4());episode={'rollout_id':'vip-worker-fault-'+uuid4().hex,'attempt':0}
    bound={'resources_session_id':sid,'episode_id':episode}
    task={'taskset':'toolathlon_gym:vip-inflight-worker-fault','task_id':TASK_ID};seed={**bound,'task_id':task,'task_data':{}}
    mutation={**bound,'request_id':str(uuid4()),'server':'email','name':'send_email',
        'arguments':{'to':'owned-fault@example.com','subject':'Owned inflight probe','body':'Owned local PG message; no external delivery.'}}
    checks=[];proof={};retained={};profile=None;fresh_profile=None;cluster=None;worker=None;children={};thread=None;ready=False;fresh_ready=False;cleanup=[];before=None;after=None
    def check(i,condition):
        checks.append({'name':CHECKS[i],'passed':bool(condition)})
        if not condition:raise AssertionError(CHECKS[i])
    def measure(target,identifier):return target.post('/isolated/verify',{'episode_id':episode,'task_id':task,'verification_input':{'resources_session_id':identifier}})['result']
    def execute(sql):
        c=cluster.connect()
        try:
            c.rollback();c.autocommit=True
            with c.cursor() as cur:cur.execute(sql)
        finally:c.close()
    def copy_file(source,name):(output/name).write_bytes(source.read_bytes())
    try:
        state=bridge_request('/status')
        if state['active_session'] or state['isolated']['active_sessions'] or state['synthetic']['active_sessions']:raise ResourceError('Close all resources before fault probe')
        before={'office':shared_snapshot(),'email_sha256':shared_email_digest()}
        client.post('/isolated/seed_session',seed);ready=True
        base=ROOT/'.runtime/native-episodes'/sid;record=json.loads((base/'cluster_owner.json').read_text())
        profile=EpisodeProfile(ROOT,sid,record['slot']);cluster=EpisodeCluster(profile);cluster.require_owner()
        deps=json.loads((base/'dependent_processes.json').read_text());leasefile=base.parent/('slot-'+str(profile.slot)+'.lock')
        lease=json.loads(leasefile.read_text());proof['worker_before']=lease['process']
        if lease['workspace_owner']!=str(ROOT) or lease['resource']!=leasefile.relative_to(ROOT).as_posix() or deps['profile']!=profile.record():raise ResourceError('Fault ownership differs')
        worker=LinuxAttestedChild(lease['process'])
        for server,child in deps['children'].items():children[server]=LinuxAttestedChild(child['before'])
        check(0,set(children)=={'woocommerce','google_sheet','email'})
        copy_file(base/'dependent_processes.json','before-dependents.json');copy_file(leasefile,'before-lease.json')
        sql="CREATE FUNCTION email.workenv_fault_pause() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(30); RAISE EXCEPTION 'owned delayed log failure'; RETURN NEW; END $$; CREATE TRIGGER workenv_fault_pause BEFORE INSERT ON email.sent_log FOR EACH ROW EXECUTE FUNCTION email.workenv_fault_pause();"
        (output/'owned-delay-trigger.sql').write_text(sql);execute(sql)
        for cookie in client.cookies:caller.cookies.set_cookie(copy.copy(cookie))
        outcome={}
        def send():
            try:outcome['reply']=caller.post('/isolated/tool',mutation,expected=503)
            except BaseException as exc:outcome['error']=type(exc).__name__+': '+str(exc)[:300]
        thread=threading.Thread(target=send,daemon=True);thread.start()
        deadline=time.monotonic()+15;waiting=False
        while time.monotonic()<deadline:
            pending=snapshot(cluster)
            c=cluster.connect()
            try:
                with c.cursor() as cur:
                    cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event='PgSleep'")
                    waiting=cur.fetchone()[0]>0
            finally:c.close()
            if len(pending['email']['messages'])==1 and not pending['email']['sent_log'] and waiting:break
            time.sleep(.05)
        check(1,len(pending['email']['messages'])==1 and not pending['email']['sent_log'] and waiting)
        save_snapshot(ROOT,output/'pending.json',pending,base/'vip-components-run/initial_snapshot.json')
        internal=request_identity(sid,mutation['request_id'])
        for folder in ('vip-components-run','email-components-run'):copy_file(base/folder/'requests.json','prepared-'+folder+'.json')
        check(2,all(json.loads((base/f/'requests.json').read_text())['entries'][internal]['state']=='prepared' for f in ('vip-components-run','email-components-run')))
        copy_file(base/'resources-session.json','before-worker-ledger.json')
        proof['worker_terminated']=worker.terminate_if_alive();proof['worker_after']=process_identity(lease['process']['pid'])
        check(3,proof['worker_terminated'] and proof['worker_after']['status'] in ('not_found','exited'))
        thread.join(10);proof['inflight_outcome']=outcome
        check(4,not thread.is_alive() and 'error' not in outcome and caller.journal[-1]['status']==503)
        try:recover_orphan_cluster(ROOT,slot=profile.slot,resource_id=sid)
        except ResourceError:blocked=True
        else:blocked=False
        check(5,blocked and (profile.data/'postmaster.pid').exists())
        for handle in children.values():handle.terminate_if_alive()
        proof['children_after']={s:process_identity(deps['children'][s]['before']['pid']) for s in children}
        check(6,all(v['status'] in ('not_found','exited') for v in proof['children_after'].values()))
        proof['reconciliation']=reconcile_dependents(ROOT,resource_id=sid,slot=profile.slot,recover_stale_locks=True)
        check(7,proof['reconciliation']['requests_replayed']==0 and proof['reconciliation']['processes_terminated']==0 and not proof['reconciliation']['slot_released'])
        client.post('/isolated/tool',mutation,expected=503)
        client.post('/isolated/tool',{**mutation,'request_id':str(uuid4())},expected=503)
        unknown=measure(client,sid);check(8,unknown['reward'] is None and unknown['mask_sample'] is True)
        frozen=snapshot(cluster);save_snapshot(ROOT,output/'frozen.json',frozen,base/'vip-components-run/initial_snapshot.json')
        check(9,frozen==pending and all((base/f/'requests.json').read_bytes()==(output/('prepared-'+f+'.json')).read_bytes() for f in ('vip-components-run','email-components-run')))
        client.post('/isolated/close_session',bound);client.post('/isolated/seed_session',seed,expected=409)
        unknown=measure(client,sid);proof['gateway_closed_row']=json.loads((VM/'isolated-sessions.json').read_text())['sessions'][sid]
        check(10,proof['gateway_closed_row']['state']=='closed' and unknown['reward'] is None and not (profile.data/'postmaster.pid').exists())
        fresh_client.post('/isolated/seed_session',{**seed,'resources_session_id':fresh})
        fresh_ready=True
        fresh_owner=json.loads((ROOT/'.runtime/native-episodes'/fresh/'cluster_owner.json').read_text())
        fresh_profile=EpisodeProfile(ROOT,fresh,fresh_owner['slot']);proof['fresh_profile']=fresh_profile.record()
        initial=snapshot(EpisodeCluster(fresh_profile))
        fresh_result=measure(fresh_client,fresh);fresh_client.post('/isolated/close_session',{**bound,'resources_session_id':fresh})
        check(11,fresh_result['reward']==0 and fresh_result['mask_sample'] is False and not initial['email']['messages']
            and not initial['office']['cells'] and initial['office']['readonly_sha256']==pending['office']['readonly_sha256'])
        after={'office':shared_snapshot(),'email_sha256':shared_email_digest()}
        check(12,before==after and not bridge_request('/status')['isolated']['active_sessions'])
    finally:
        try:
            if worker is not None:worker.terminate_if_alive()
            for h in children.values():h.terminate_if_alive()
            if profile is not None:
                try:require_dependents_closed(profile)
                except ResourceError:reconcile_dependents(ROOT,resource_id=sid,slot=profile.slot,recover_stale_locks=True)
            if ready:client.post('/isolated/close_session',bound)
            if fresh_ready:fresh_client.post('/isolated/close_session',{**bound,'resources_session_id':fresh})
        except Exception as exc:cleanup.append(type(exc).__name__+': '+str(exc)[:200])
        if thread is not None:thread.join(5)
        if worker is not None:worker.close()
        for h in children.values():h.close()
        for p in (profile,fresh_profile):
            if p is None:continue
            for f in p.base.rglob('*'):
                if f.is_file() and 'pgdata' not in f.parts and f.suffix in ('.json','.txt','.py','.mjs'):retained[f.relative_to(ROOT).as_posix()]=hashlib.sha256(f.read_bytes()).hexdigest()
        save(output/'observations.json',proof);save(output/'http_journal.json',client.journal+caller.journal+fresh_client.journal)
        for f in output.iterdir():
            if f.is_file():retained[f.relative_to(ROOT).as_posix()]=hashlib.sha256(f.read_bytes()).hexdigest()
        r={'schema_version':'nemo-vip-inflight-worker-fault-v1','at':datetime.now(timezone.utc).isoformat(),
            'passed':[c['name'] for c in checks]==list(CHECKS) and all(c['passed'] for c in checks) and not cleanup,
            'checks':checks,'cleanup_errors':cleanup,'profile':profile.record() if profile else None,'seed':seed,'mutation':mutation,
            'fresh_resource_id':fresh,'retained_sha256':retained,'shared_before':before,'shared_after':after,
            'real_model_calls':0,'training_updates':0,'training_admissions':0,'external_messages_sent':0,'training_eligible':False,
            'source_sha256':{**expected_identity(ROOT),**{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in EXTRA_SOURCES}}}
        save(output/'result.json',r);print(json.dumps({'passed':r['passed'],'checks':len(checks),'receipt':(output/'result.json').relative_to(ROOT).as_posix()}))
    print(json.dumps(audit_fault(ROOT,output/'result.json')))


if __name__=='__main__':main()
