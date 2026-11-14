"""Actual owned cloud/mail adapter probe; task routing stays disabled."""
import asyncio
from contextlib import AsyncExitStack, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from adapters import vip_email_validation as validation
from integrations.vip_email_components import decode, jsonable
from integrations.vip_email_resource import VipEmailResourceAdapter
from integrations.vip_resource_audit import CHECKS, SOURCES, audit_preparation
from integrations.email_components import snapshot
from integrations.episode_cluster import reserve_cluster, require_dependents_closed
from scripts.verify_calendar_isolation import shared_snapshot
from scripts.verify_email_components import shared_email_digest
from workenv.core import ResourceError, contained, save


def main():
    output=ROOT/'evidence/mainline'/('vip-resource-adapter-'+uuid4().hex);output.mkdir()
    checks=[];clusters=[];adapters=[];measurements=[];retained={};error=None;after=None
    before={'office':shared_snapshot(),'email_sha256':shared_email_digest()}
    ledgers={n:(ROOT/'.runtime/nemo-vm'/n).read_bytes() for n in ('resource-sessions.json','isolated-sessions.json','synthetic-sessions.json')}
    for n,raw in ledgers.items():(output/('ledger-before-'+n)).write_bytes(raw)
    def check(index,condition):
        checks.append({'name':CHECKS[index],'passed':bool(condition)})
        if not condition:raise AssertionError(CHECKS[index])
    def execute(cluster,sql):
        connection=cluster.connect()
        try:
            connection.rollback();connection.autocommit=True
            with connection.cursor() as cursor:cursor.execute(sql)
        finally:connection.close()
    async def measure(index):
        result,path=await adapters[index].verify()
        measurements.append({'index':index,'result':result,'evaluation_ref':path.relative_to(ROOT).as_posix()})
        return result
    traces=[[],[]]
    async def call(index,server,name,args,identity=None):
        return await adapters[index].call_request(server,name,args,identity or str(uuid4()),traces[index])
    async def close_open(adapter):
        if adapter.context is not None:await adapter.close(adapter.run)
    async def run():
        profiles=json.loads((ROOT/'configs/task_adapter_profiles.json').read_text())['profiles']
        check(0,all(p['training_admission'] is False for p in profiles.values()))
        seed=json.loads((ROOT/'.runtime/nemo-vm/email-component-seed.json').read_text());sql=contained(ROOT,seed['seed_ref'])
        if hashlib.sha256(sql.read_bytes()).hexdigest()!=seed['sha256']:raise ResourceError('VIP adapter seed changed')
        with ExitStack() as owned:
            for slot in (0,1):
                c=owned.enter_context(reserve_cluster(ROOT,slot=slot,resource_id=str(uuid4())));c.start(sql);clusters.append(c)
            adapters.extend(VipEmailResourceAdapter(ROOT,c) for c in clusters)
            async with AsyncExitStack() as services:
                for i in (1,0):
                    await adapters[i].seed(adapters[i].new_run());services.push_async_callback(close_open,adapters[i])
                check(1,all(not snapshot(c)['office']['cells'] and not any(snapshot(c)['email'].values()) for c in clusters))
                check(2,all(len(a.tools()['tools'])==10 and a.tools()['artifact_tools']==[] for a in adapters))
                refused=0
                for operation in (adapters[0].read(),adapters[0].write({})):
                    try:await operation
                    except ValueError:refused+=1
                check(3,refused==2)
                empty=[await measure(i) for i in (0,1)]
                check(4,all(r['task_passed'] is False and r['evaluation_status']=='completed' for r in empty))
                source=json.loads((adapters[0].components.run/'original_customers.json').read_text())
                truth=validation.expected(source['customers'],source_binding=validation.source_identity())
                rid=str(uuid4());created=await call(0,'google_sheet','create_spreadsheet',{'title':validation.TITLE},rid)
                sid=decode(created)['spreadsheetId']
                tabs=await call(0,'google_sheet','list_sheets',{'spreadsheet_id':sid})
                names=[c['text'] for c in tabs['content'] if c['type']=='text']
                if len(names)!=1:raise ResourceError('VIP adapter original default tab differs')
                await call(0,'google_sheet','rename_sheet',{'spreadsheet':sid,'sheet':names[0],'new_name':'Top Customers'})
                await call(0,'google_sheet','create_sheet',{'spreadsheet_id':sid,'title':'Store Summary'})
                for title,headers in validation.HEADERS.items():
                    rows=truth['rows'] if title=='Top Customers' else [{'Metric':k,'Value':v} for k,v in truth['metrics'].items()]
                    grid=jsonable([list(headers),*[[row[k] for k in headers] for row in rows]])
                    await call(0,'google_sheet','update_cells',{'spreadsheet_id':sid,'sheet':title,'range':'A1:'+('E11' if title=='Top Customers' else 'B5'),'data':grid})
                args=[];mail_replay=None
                for address,first in truth['recipients'].items():
                    args.append({'to':address,'subject':validation.SUBJECT,'body':'Dear '+first+', thank you for your loyalty and patronage.'})
                    identity=str(uuid4());reply=await call(0,'email','send_email',args[-1],identity)
                    if mail_replay is None:mail_replay=(identity,deepcopy(reply))
                check(5,(await measure(0))['task_passed'] is True)
                state=snapshot(clusters[0]);cached=await call(0,'google_sheet','create_spreadsheet',{'title':validation.TITLE},rid)
                mid,mreply=mail_replay;mail_cached=await call(0,'email','send_email',args[0],mid)
                try:await call(0,'email','send_email',{**args[0],'body':'conflicting retry'},mid)
                except ValueError:conflict=True
                else:conflict=False
                check(6,cached==created and mail_cached==mreply and conflict and snapshot(clusters[0])==state)
                try:await call(1,'google_sheet','update_cells',{'spreadsheet_id':sid,'sheet':'Top Customers','range':'D2:D2','data':[['0']]})
                except ResourceError:foreign=True
                else:foreign=False
                check(7,foreign and not adapters[1].components.unknown)
                total=truth['rows'][0]['Total_Spent']
                await call(0,'google_sheet','update_cells',{'spreadsheet_id':sid,'sheet':'Top Customers','range':'D2:D2','data':[[str(total+validation.Decimal('0.01'))]]})
                bad=await measure(0)
                check(8,bad['original_passed'] is True and bad['supplemental_passed'] is False and bad['task_passed'] is False)
                await call(0,'google_sheet','update_cells',{'spreadsheet_id':sid,'sheet':'Top Customers','range':'D2:D2','data':[[str(total)]]})
                check(9,(await measure(0))['task_passed'] is True)
                second=snapshot(clusters[1]);await adapters[0].close(adapters[0].run)
                check(10,snapshot(clusters[1])==second and adapters[1].components.opened)
                fault=str(uuid4());execute(clusters[1],'ALTER TABLE email.sent_log ADD CONSTRAINT vip_resource_reject_log CHECK (false) NOT VALID')
                try:
                    try:await call(1,'email','send_email',args[0],fault)
                    except ResourceError:unknown=True
                    else:unknown=False
                finally:execute(clusters[1],'ALTER TABLE email.sent_log DROP CONSTRAINT vip_resource_reject_log')
                frozen=snapshot(clusters[1])
                check(11,unknown and len(frozen['email']['messages'])==1 and not frozen['email']['sent_log'])
                blocked=0
                for identity in (fault,str(uuid4())):
                    try:await call(1,'email','send_email',args[0],identity)
                    except ResourceError:blocked+=1
                try:await measure(1)
                except ResourceError:blocked+=1
                check(12,blocked==3 and snapshot(clusters[1])==frozen)
                await adapters[1].close(adapters[1].run)
                closed=json.loads((adapters[1].run/'closed.json').read_text())
                check(13,closed['outcome_unknown'] and not closed['reset_performed'] and snapshot(clusters[1])==frozen)
            for c in clusters:require_dependents_closed(c.profile)
            check(14,all(len(a.components.children)==3 and all(v['closed'] for v in a.components.children.values()) for a in adapters))
        check(15,all(not (c.profile.data/'postmaster.pid').exists() for c in clusters))
    try:
        asyncio.run(run());after={'office':shared_snapshot(),'email_sha256':shared_email_digest()}
        check(16,before==after and all((ROOT/'.runtime/nemo-vm'/n).read_bytes()==raw for n,raw in ledgers.items()))
    except BaseException as exc:
        error=type(exc).__name__+': '+str(exc)[:400];raise
    finally:
        for n in ledgers:(output/('ledger-after-'+n)).write_bytes((ROOT/'.runtime/nemo-vm'/n).read_bytes())
        for c in clusters:
            for p in c.profile.base.rglob('*'):
                if p.is_file() and 'pgdata' not in p.parts and p.suffix in ('.json','.txt','.py','.mjs'):
                    retained[p.relative_to(ROOT).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
        for p in output.iterdir():
            if p.is_file():retained[p.relative_to(ROOT).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
        result={'schema_version':'vip-resource-adapter-preparation-v1','at':datetime.now(timezone.utc).isoformat(),
            'passed':error is None and [c['name'] for c in checks]==list(CHECKS) and all(c['passed'] for c in checks),
            'checks':checks,'error':error,'profiles':[c.profile.record() for c in clusters],'measurements':measurements,
            'retained_sha256':retained,'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in SOURCES},
            'shared_before':before,'shared_after':after,'runtime_task_activated':False,'nemo_task_executed':False,
            'task_passed':None,'reward':None,'real_model_calls':0,'training_updates':0,'training_admissions':0,'external_messages_sent':0}
        save(output/'result.json',result)
        print(json.dumps({'passed':result['passed'],'checks':len(checks),'receipt':(output/'result.json').relative_to(ROOT).as_posix()}))
    print(json.dumps(audit_preparation(ROOT,output/'result.json')))


if __name__=='__main__':main()
