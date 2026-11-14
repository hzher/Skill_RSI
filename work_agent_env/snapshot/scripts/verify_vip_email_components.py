"""Exercise complete original VIP business components, with local PG mail only."""
import asyncio
from contextlib import AsyncExitStack, ExitStack
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from adapters import vip_email_validation as validation
from adapters.vip_email_evaluation import evaluate,reset
from integrations.vip_email_components import VipEmailComponents,collect_sources,decode,jsonable,sql_truth
from integrations.vip_email_audit import CHECKS,SOURCES
from integrations.email_components import snapshot
from integrations.episode_cluster import reserve_cluster,require_dependents_closed
from scripts.verify_calendar_isolation import shared_snapshot
from scripts.verify_email_components import shared_email_digest
from workenv.core import ResourceError,contained,save


def main():
    output=ROOT/'evidence/mainline'/('vip-components-'+uuid4().hex);output.mkdir()
    checks,clusters,controllers,measurements,retained=[],[],[],[],{}
    before={'office':shared_snapshot(),'email_sha256':shared_email_digest()};after=None;error=None
    ledgers={n:(ROOT/'.runtime/nemo-vm'/n).read_bytes() for n in ('resource-sessions.json','isolated-sessions.json','synthetic-sessions.json')}
    for n,raw in ledgers.items():(output/('ledger-before-'+n)).write_bytes(raw)
    def check(index,condition):
        checks.append({'name':CHECKS[index],'passed':bool(condition)})
        if not condition:raise AssertionError(CHECKS[index])
    def execute(cluster,statement,args=()):
        connection=cluster.connect()
        try:
            connection.rollback();connection.autocommit=True
            with connection.cursor() as cur:cur.execute(statement,args)
        finally:connection.close()
    def measure(i):
        result=evaluate(controllers[i]);measurements.append({'index':i,'result':result});return result
    async def call(i,server,name,args,identity=None):return await controllers[i].call(server,name,args,identity or str(uuid4()))
    async def close_open(context,controller):
        if controller.opened:await context.__aexit__(None,None,None)
    async def run():
        profiles=json.loads((ROOT/'configs/task_adapter_profiles.json').read_text())['profiles']
        check(0,all(p['training_admission'] is False for p in profiles.values()))
        build=json.loads((ROOT/'evidence/mainline/email_build.json').read_text())
        seed=json.loads((ROOT/'.runtime/nemo-vm/email-component-seed.json').read_text());sql=contained(ROOT,seed['seed_ref'])
        if hashlib.sha256(sql.read_bytes()).hexdigest()!=seed['sha256']:raise ResourceError('VIP seed changed')
        with ExitStack() as owned:
            for slot in (0,1):
                c=owned.enter_context(reserve_cluster(ROOT,slot=slot,resource_id=str(uuid4())));c.start(sql);clusters.append(c)
            controllers.extend([None,None]);contexts=[None,None]
            async with AsyncExitStack() as services:
                for i in (1,0):
                    context=VipEmailComponents(ROOT,clusters[i],build).open();controller=await context.__aenter__()
                    contexts[i]=context;controllers[i]=controller;services.push_async_callback(close_open,context,controller)
                data=[snapshot(c) for c in clusters]
                check(1,all(not d['office']['cells'] and not any(d['email'].values()) for d in data))
                check(2,all(len(c.schemas)==10 and len(c.children)==3 for c in controllers))
                truth=[]
                for controller in controllers:
                    truth.append(await collect_sources(controller,lambda:str(uuid4())))
                    sql_truth(controller.cluster,snapshot(controller.cluster))
                check(3,all((c.run/'original_customers.json').is_file() for c in controllers))
                check(4,all(measure(i)['component_measurement_passed'] is False for i in (0,1)))
                ids=[];mail_args=[];create_replay=None
                for i in (0,1):
                    rid=str(uuid4());created=await call(i,'google_sheet','create_spreadsheet',{'title':validation.TITLE},rid)
                    if i==0:create_replay=(rid,deepcopy(created))
                    sid=decode(created)['spreadsheetId'];ids.append(sid)
                    tabs=await call(i,'google_sheet','list_sheets',{'spreadsheet_id':sid})
                    names=[b['text'] for b in tabs['content'] if b['type']=='text']
                    if len(names)!=1:raise ResourceError('VIP default tab inventory differs')
                    await call(i,'google_sheet','rename_sheet',{'spreadsheet':sid,'sheet':names[0],'new_name':'Top Customers'})
                    await call(i,'google_sheet','create_sheet',{'spreadsheet_id':sid,'title':'Store Summary'})
                    for title,headers in validation.HEADERS.items():
                        rows=truth[i]['rows'] if title=='Top Customers' else [{'Metric':k,'Value':v} for k,v in truth[i]['metrics'].items()]
                        grid=jsonable([list(headers),*[[row[k] for k in headers] for row in rows]])
                        await call(i,'google_sheet','update_cells',{'spreadsheet_id':sid,'sheet':title,'range':'A1:'+('E11' if title=='Top Customers' else 'B5'),'data':grid})
                    args=[]
                    for address,first in truth[i]['recipients'].items():
                        args.append({'to':address,'subject':validation.SUBJECT,'body':'Dear '+first+', thank you for your loyalty and patronage.'})
                        identity=str(uuid4());reply=await call(i,'email','send_email',args[-1],identity)
                        if i==0 and len(args)==1:mail_replay=(identity,deepcopy(reply))
                    mail_args.append(args)
                check(5,ids[0]!=ids[1] and all(measure(i)['component_measurement_passed'] is True for i in (0,1)))
                known=snapshot(clusters[0]);rid,reply=create_replay
                assert await call(0,'google_sheet','create_spreadsheet',{'title':validation.TITLE},rid)==reply
                mid,mreply=mail_replay;assert await call(0,'email','send_email',mail_args[0][0],mid)==mreply
                try:await call(0,'email','send_email',{**mail_args[0][0],'body':'conflict'},mid)
                except ValueError:conflict=True
                else:conflict=False
                check(6,conflict and snapshot(clusters[0])==known)
                for server,name,args in (('google_sheet','update_cells',{'spreadsheet_id':ids[1],'sheet':'Top Customers','range':'D2:D2','data':[['0']]}),('woocommerce','woo_customers_delete',{'id':1})):
                    try:await call(0,server,name,args)
                    except (ValueError,ResourceError):pass
                    else:raise AssertionError('Foreign or unallowlisted VIP operation accepted')
                check(7,snapshot(clusters[0])==known)
                total=truth[0]['rows'][0]['Total_Spent']
                await call(0,'google_sheet','update_cells',{'spreadsheet_id':ids[0],'sheet':'Top Customers','range':'D2:D2','data':[[str(total+validation.CENT)]]})
                bad=measure(0);check(8,bad['original_passed'] is True and bad['supplemental']['supplemental_passed'] is False)
                await call(0,'google_sheet','update_cells',{'spreadsheet_id':ids[0],'sheet':'Top Customers','range':'D2:D2','data':[[str(total)]]})
                headers=validation.HEADERS['Top Customers'];original=truth[0]['rows'][-1]
                await call(0,'google_sheet','update_cells',{'spreadsheet_id':ids[0],'sheet':'Top Customers','range':'A11:E11','data':jsonable([[truth[0]['rows'][-2][k] for k in headers]])})
                bad=measure(0);check(9,bad['original_passed'] is True and bad['supplemental']['supplemental_passed'] is False)
                await call(0,'google_sheet','update_cells',{'spreadsheet_id':ids[0],'sheet':'Top Customers','range':'A11:E11','data':jsonable([[original[k] for k in headers]])})
                execute(clusters[0],'ALTER TABLE email.sent_log RENAME TO sent_log_fixture')
                execute(clusters[0],'CREATE TABLE email.sent_log (LIKE email.sent_log_fixture INCLUDING ALL)')
                try:bad=measure(0)
                finally:
                    execute(clusters[0],'DROP TABLE email.sent_log');execute(clusters[0],'ALTER TABLE email.sent_log_fixture RENAME TO sent_log')
                check(10,bad['original_passed'] is True and bad['supplemental']['supplemental_passed'] is False)
                row=snapshot(clusters[0])['email']['messages'][0]
                execute(clusters[0],'UPDATE email.messages SET to_addr=%s::jsonb WHERE id=%s',(json.dumps(['wrong@example.com']),row['id']))
                bad=measure(0);execute(clusters[0],'UPDATE email.messages SET to_addr=%s::jsonb WHERE id=%s',(json.dumps(row['to_addr']),row['id']))
                check(11,bad['original_passed'] is False and bad['supplemental']['supplemental_passed'] is False)
                old_ids={m['id'] for m in snapshot(clusters[0])['email']['messages']}
                await call(0,'email','send_email',mail_args[0][0]);bad=measure(0)
                added=[m['id'] for m in snapshot(clusters[0])['email']['messages'] if m['id'] not in old_ids]
                execute(clusters[0],'DELETE FROM email.sent_log WHERE message_id=%s',(added[0],));execute(clusters[0],'DELETE FROM email.messages WHERE id=%s',(added[0],))
                check(12,bad['original_passed'] is True and bad['supplemental']['supplemental_passed'] is False)
                check(13,measure(0)['component_measurement_passed'] is True)
                marker=clusters[0].profile.base/'email-initialized.json';raw=marker.read_bytes();changed=json.loads(raw);changed['profile']={};save(marker,changed)
                try:
                    try:measure(0)
                    except ResourceError:refused=True
                    else:refused=False
                finally:marker.write_bytes(raw)
                check(14,refused)
                await contexts[0].__aexit__(None,None,None)
                check(15,measure(1)['component_measurement_passed'] is True and not controllers[0].opened)
                other=snapshot(clusters[1]);cleared=reset(ROOT,clusters[0])
                check(16,not cleared['office']['cells'] and not cleared['email']['messages'] and snapshot(clusters[1])==other)
                fault_id=str(uuid4());execute(clusters[1],'ALTER TABLE gsheet.cells ADD CONSTRAINT vip_reject_write CHECK (false) NOT VALID')
                try:
                    try:await call(1,'google_sheet','update_cells',{'spreadsheet_id':ids[1],'sheet':'Top Customers','range':'D2:D2','data':[['0']]},fault_id)
                    except ResourceError:unknown=True
                    else:unknown=False
                finally:execute(clusters[1],'ALTER TABLE gsheet.cells DROP CONSTRAINT vip_reject_write')
                check(17,unknown and controllers[1].unknown)
                frozen=snapshot(clusters[1]);attempts=0
                for identity in (fault_id,str(uuid4())):
                    try:await call(1,'google_sheet','update_cells',{'spreadsheet_id':ids[1],'sheet':'Top Customers','range':'D2:D2','data':[['0']]},identity)
                    except ResourceError:attempts+=1
                try:measure(1)
                except ResourceError:attempts+=1
            try:reset(ROOT,clusters[1])
            except ResourceError:attempts+=1
            check(18,attempts==4 and snapshot(clusters[1])==frozen)
            for c in clusters:require_dependents_closed(c.profile)
            check(19,all(len(c.children)==3 and all(k['closed'] for k in c.children.values()) for c in controllers))
        check(20,all(not (c.profile.data/'postmaster.pid').exists() for c in clusters))
    try:
        asyncio.run(run());after={'office':shared_snapshot(),'email_sha256':shared_email_digest()}
        check(21,before==after and all((ROOT/'.runtime/nemo-vm'/n).read_bytes()==raw for n,raw in ledgers.items()))
    except BaseException as exc:
        error=type(exc).__name__+': '+str(exc)[:400];raise
    finally:
        for n in ledgers:(output/('ledger-after-'+n)).write_bytes((ROOT/'.runtime/nemo-vm'/n).read_bytes())
        for c in clusters:
            for p in c.profile.base.rglob('*'):
                if p.is_file() and 'pgdata' not in p.parts and p.suffix in ('.json','.txt','.py','.mjs'):retained[p.relative_to(ROOT).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
        for p in output.iterdir():
            if p.is_file():retained[p.relative_to(ROOT).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
        result={'schema_version':'vip-original-business-components-diagnostic-v1','at':datetime.now(timezone.utc).isoformat(),
            'passed':error is None and [c['name'] for c in checks]==list(CHECKS) and all(c['passed'] for c in checks),'checks':checks,'error':error,
            'profiles':[c.profile.record() for c in clusters],'measurements':measurements,'retained_sha256':retained,
            'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in SOURCES},'shared_before':before,'shared_after':after,
            'runtime_task_activated':False,'task_passed':None,'reward':None,'real_model_calls':0,'training_updates':0,'training_admissions':0,'external_messages_sent':0}
        save(output/'result.json',result);print(json.dumps({'passed':result['passed'],'checks':len(checks),'receipt':(output/'result.json').relative_to(ROOT).as_posix()}))


if __name__=='__main__':main()
