"""Actual NeMo VIP cloud/mail task, with local PG messages only and no model."""
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import json
import sys
from uuid import uuid4

from mainline_runtime import ROOT,VM,bridge_request
sys.path.insert(0,str(ROOT))
from adapters import vip_email_validation as validation
from integrations.vip_email_components import METHODS,decode,jsonable
from integrations.vip_email_resource import TASK_ID,REFERENCE_MODE,REWARD_VERSION
from integrations.vip_runtime_audit import CHECKS,EXTRA_SOURCES,audit_vip
from integrations.email_components import snapshot
from integrations.episode_cluster import EpisodeProfile,EpisodeCluster,require_dependents_closed
from integrations.nemo_http import NeMoHTTP
from integrations.runtime_identity import expected_identity
from scripts.verify_calendar_isolation import shared_snapshot
from scripts.verify_email_components import shared_email_digest
from workenv.core import save


def main():
    output=ROOT/'evidence/mainline'/('nemo-vip-'+uuid4().hex);output.mkdir()
    clients=[NeMoHTTP() for _ in range(3)]
    seeds=[{'resources_session_id':str(uuid4()),'episode_id':{'rollout_id':'vip-'+uuid4().hex,'attempt':0},
        'task_id':{'taskset':'toolathlon_gym:nemo-vip-diagnostic','task_id':task},'task_data':{}} for task in (TASK_ID,TASK_ID,'howtocook-meal-plan-gcal')]
    checks=[];active=[];profiles={};measurements=[];retained={};error=None;after=None
    before={'office':shared_snapshot(),'email_sha256':shared_email_digest()}
    other={n:(VM/n).read_bytes() for n in ('resource-sessions.json','synthetic-sessions.json')}
    def check(index,condition):
        checks.append({'name':CHECKS[index],'passed':bool(condition)})
        if not condition:raise AssertionError(CHECKS[index])
    def bound(i):return {k:seeds[i][k] for k in ('resources_session_id','episode_id')}
    def post(i,route,body,expected=200):return clients[i].post('/isolated'+route,body,expected=expected)
    def seed(i):
        post(i,'/seed_session',seeds[i]);active.append(i)
        sid=seeds[i]['resources_session_id'];record=json.loads((ROOT/'.runtime/native-episodes'/sid/'cluster_owner.json').read_text())
        profiles[i]=EpisodeProfile(ROOT,sid,record['slot'])
    def measure(i):
        result=post(i,'/verify',{'episode_id':seeds[i]['episode_id'],'task_id':seeds[i]['task_id'],
            'verification_input':{'resources_session_id':seeds[i]['resources_session_id']}})['result']
        measurements.append({'index':i,'result':result});return result
    def tool(i,server,name,args,identity=None,expected=200):
        return post(i,'/tool',{**bound(i),'request_id':identity or str(uuid4()),'server':server,'name':name,'arguments':args},expected)
    def data(i):return snapshot(EpisodeCluster(profiles[i]))
    def execute(i,sql,args=()):
        c=EpisodeCluster(profiles[i]).connect()
        try:
            c.rollback();c.autocommit=True
            with c.cursor() as cur:cur.execute(sql,args)
        finally:c.close()
    cloud={};truth={};mail_args={};replay={}
    try:
        clients[2].post('/seed_session',seeds[0],expected=400);check(0,not bridge_request('/status')['isolated']['active_sessions'])
        for i in (0,1):seed(i)
        check(1,set(bridge_request('/status')['isolated']['active_sessions'])=={s['resources_session_id'] for s in seeds[:2]})
        exposed=post(0,'/tools',bound(0))
        check(2,len(exposed['tools'])==10 and exposed['artifact_tools']==[] and exposed['task_profile']['original_service_aliases']=={'email':'emails'}
            and exposed['task_profile']['reward_version']==REWARD_VERSION)
        clients[2].post('/isolated/tools',bound(0),expected=409)
        tool(0,'email','delete_email',{},expected=400);check(3,True)
        check(4,all(measure(i)['reward']==0 for i in (0,1)))
        for i in (0,1):
            rows=[]
            for page in range(1,65):
                values=decode(tool(i,'woocommerce','woo_customers_list',{'page':page,'perPage':10})['result']);rows.extend(values)
                if len(values)<10:break
            else:raise AssertionError('VIP original pagination budget')
            from integrations.vip_email_components import check_customers
            check_customers(rows,data(i));truth[i]=validation.expected(rows,source_binding=validation.source_identity())
        check(5,all(len(t['rows'])==10 and len(t['recipients'])==5 for t in truth.values()))
        for i in (0,1):
            identity=str(uuid4());args={'title':validation.TITLE};created=tool(i,'google_sheet','create_spreadsheet',args,identity)
            replay[i]=(identity,args,created);sid=decode(created['result'])['spreadsheetId'];cloud[i]=sid
            tabs=tool(i,'google_sheet','list_sheets',{'spreadsheet_id':sid})['result']
            names=[c['text'] for c in tabs['content'] if c['type']=='text']
            if len(names)!=1:raise AssertionError('VIP default tab inventory')
            tool(i,'google_sheet','rename_sheet',{'spreadsheet':sid,'sheet':names[0],'new_name':'Top Customers'})
            tool(i,'google_sheet','create_sheet',{'spreadsheet_id':sid,'title':'Store Summary'})
            for title,headers in validation.HEADERS.items():
                rows=truth[i]['rows'] if title=='Top Customers' else [{'Metric':k,'Value':v} for k,v in truth[i]['metrics'].items()]
                grid=jsonable([list(headers),*[[r[k] for k in headers] for r in rows]])
                tool(i,'google_sheet','update_cells',{'spreadsheet_id':sid,'sheet':title,'range':'A1:'+('E11' if title=='Top Customers' else 'B5'),'data':grid})
            mail_args[i]=[]
            for address,first in truth[i]['recipients'].items():
                args={'to':address,'subject':validation.SUBJECT,'body':'Dear '+first+', thank you for your loyalty and patronage.'}
                identity=str(uuid4());reply=tool(i,'email','send_email',args,identity);mail_args[i].append((identity,args,reply))
        check(6,all(measure(i)['reward']==1 for i in (0,1)))
        frozen=data(0);rid,args,reply=replay[0];cached=tool(0,'google_sheet','create_spreadsheet',args,rid)
        mid,margs,mreply=mail_args[0][0];mcached=tool(0,'email','send_email',margs,mid)
        tool(0,'email','send_email',{**margs,'body':'conflict'},mid,409)
        check(7,cached==reply and mcached==mreply and data(0)==frozen)
        tool(0,'google_sheet','update_cells',{'spreadsheet_id':cloud[1],'sheet':'Top Customers','range':'D2:D2','data':[['0']]},expected=503)
        check(8,data(0)==frozen)
        def amount(value):tool(0,'google_sheet','update_cells',{'spreadsheet_id':cloud[0],'sheet':'Top Customers','range':'D2:D2','data':[[str(value)]]})
        total=truth[0]['rows'][0]['Total_Spent'];amount(total+Decimal('.01'));bad=measure(0)
        check(9,bad['reward']==0 and bad['evaluation']['original_passed'] is True and measure(1)['reward']==1)
        amount(total)
        execute(0,'ALTER TABLE email.sent_log RENAME TO sent_log_fixture');execute(0,'CREATE TABLE email.sent_log (LIKE email.sent_log_fixture INCLUDING ALL)')
        try:bad=measure(0)
        finally:execute(0,'DROP TABLE email.sent_log');execute(0,'ALTER TABLE email.sent_log_fixture RENAME TO sent_log')
        check(10,bad['reward']==0 and bad['evaluation']['original_passed'] is True)
        row=data(0)['email']['messages'][0]
        execute(0,'UPDATE email.messages SET to_addr=%s::jsonb WHERE id=%s',(json.dumps(['wrong@example.com']),row['id']))
        try:bad=measure(0)
        finally:execute(0,'UPDATE email.messages SET to_addr=%s::jsonb WHERE id=%s',(json.dumps(row['to_addr']),row['id']))
        check(11,bad['reward']==0 and bad['evaluation']['original_passed'] is False and measure(0)['reward']==1)
        marker=profiles[0].base/'email-initialized.json';raw=marker.read_bytes();changed=json.loads(raw);changed['profile']={};save(marker,changed)
        try:unknown=measure(0)
        finally:marker.write_bytes(raw)
        check(12,unknown['reward'] is None and unknown['mask_sample'] is True and measure(0)['reward']==1)
        post(0,'/close_session',bound(0));active.remove(0);check(13,measure(1)['reward']==1)
        seed(2);check(14,profiles[2].slot==profiles[0].slot and measure(2)['reward']==0)
        fault=str(uuid4());execute(1,'ALTER TABLE email.sent_log ADD CONSTRAINT vip_nemo_reject_log CHECK (false) NOT VALID')
        try:tool(1,'email','send_email',mail_args[1][0][1],fault,503)
        finally:execute(1,'ALTER TABLE email.sent_log DROP CONSTRAINT vip_nemo_reject_log')
        frozen=data(1);check(15,len(frozen['email']['messages'])==6 and len(frozen['email']['sent_log'])==5)
        tool(1,'email','send_email',mail_args[1][0][1],fault,409)
        tool(1,'email','send_email',mail_args[1][0][1],expected=503)
        unknown=measure(1);check(16,unknown['reward'] is None and unknown['mask_sample'] is True and data(1)==frozen)
        for i in (2,1):post(i,'/close_session',bound(i));active.remove(i)
        for i in (0,1):post(i,'/seed_session',seeds[i],409)
        check(17,measure(0)['reward'] is None and all(not(p.data/'postmaster.pid').exists() for p in profiles.values()))
        for p in profiles.values():require_dependents_closed(p)
        after={'office':shared_snapshot(),'email_sha256':shared_email_digest()}
        check(18,before==after and all((VM/n).read_bytes()==raw for n,raw in other.items()))
    except Exception as exc:error=type(exc).__name__+': '+str(exc)[:500];raise
    finally:
        cleanup=[]
        for i in reversed(active):
            try:post(i,'/close_session',bound(i))
            except Exception as exc:cleanup.append(type(exc).__name__)
        # Keep the authority for this layer: HTTP -> original journal -> full
        # evaluator input -> close state. The independently audited c2 probe
        # still checks every RPC before/after pair. Auxiliary files stay on disk.
        for i,p in profiles.items():
            members={p.base/'cluster_owner.json',p.base/'dependent_processes.json'}
            if i<2:
                members.add(p.base/'resources-session.json')
                members.update((p.base/'vip-task-session').glob('*.json'))
                members.update(p.base/'vip-components-run'/n for n in ('initial_snapshot.json','requests.json',
                    'tool_schemas.json','original_customers.json','woocommerce-process.json','sheets-process.json'))
                members.update(p.base/'email-components-run'/n for n in ('requests.json','email-process.json'))
                mail=json.loads((p.base/'email-components-run/requests.json').read_text())
                for entry in mail['entries'].values():
                    if entry['state']=='unknown':
                        members.update(ROOT/entry[phase+'_ref'] for phase in ('before','after') if phase+'_ref' in entry)
                members.update(f for f in (p.base/'vip-evaluations').rglob('*') if f.is_file())
            for f in members:
                if f.is_file():retained[f.relative_to(ROOT).as_posix()]=hashlib.sha256(f.read_bytes()).hexdigest()
        save(output/'http_journal.json',[e for c in clients for e in c.journal])
        for f in output.iterdir():
            if f.is_file():retained[f.relative_to(ROOT).as_posix()]=hashlib.sha256(f.read_bytes()).hexdigest()
        receipt={'schema_version':'nemo-vip-project-diagnostic-v1','at':datetime.now(timezone.utc).isoformat(),
            'passed':error is None and [c['name'] for c in checks]==list(CHECKS) and all(c['passed'] for c in checks) and not cleanup,
            'checks':checks,'error':error,'cleanup_errors':cleanup,'seeds':seeds,'profiles':{str(i):p.record() for i,p in profiles.items()},
            'measurements':measurements,'retained_sha256':retained,'shared_before':before,'shared_after':after,
            'execution_kind':'scripted_original_task_diagnostic','training_eligible':False,'real_model_calls':0,'training_updates':0,'training_admissions':0,
            'reference_mode':REFERENCE_MODE,'upstream_reference_used':False,'official_benchmark_score_available':False,'reference_solutions_read':False,'verified_task_bodies_read':False,
            'source_sha256':{**expected_identity(ROOT),**{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in EXTRA_SOURCES}}}
        save(output/'result.json',receipt);print(json.dumps({'passed':receipt['passed'],'checks':len(checks),'receipt':(output/'result.json').relative_to(ROOT).as_posix()}))
    print(json.dumps(audit_vip(ROOT,output/'result.json')))
    return 0


if __name__=='__main__':raise SystemExit(main())
