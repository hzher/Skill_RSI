"""Run pinned inventory evaluator against explicit owned SQL fixtures only."""
import asyncio
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from adapters import inventory_alert_evaluation as evaluation
from adapters import inventory_alert_artifacts as artifacts
from integrations import inventory_data as data
from integrations.episode_cluster import reserve_cluster
from integrations.order_analysis_resource import woocommerce_session
from integrations.inventory_evaluation_audit import CHECKS,CASES,SOURCES,audit
from scripts.prepare_inventory_data_seed import shared_state,validate_seed
from workenv.core import contained,save


def execute(cluster,sql,args=()):
    conn=cluster.connect()
    try:
        with conn,conn.cursor() as cursor:
            cursor.execute(sql,args)
            return cursor.fetchall() if cursor.description else None
    finally:conn.close()


def main():
    output=ROOT/'evidence/mainline'/('inventory-evaluation-'+uuid4().hex);output.mkdir(parents=True)
    checks=[];measurements=[];retained={};error=None;profile=None;child_path=None;stopped=False
    def check(index,passed):
        checks.append({'name':CHECKS[index],'passed':bool(passed)})
        if not passed:raise AssertionError(CHECKS[index])
    before=shared_state(ROOT)
    ledgers=[ROOT/'.runtime/nemo-vm'/n for n in ('resource-sessions.json','isolated-sessions.json','synthetic-sessions.json')]
    ledger_before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ledgers}
    def retain(path):retained[path.relative_to(ROOT).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    def measure(name,cluster):
        result=evaluation.evaluate(ROOT,cluster);measurements.append({'name':name,'evaluation_ref':result['evaluation_ref']})
        directory=contained(ROOT,result['evaluation_ref']).parent
        for path in directory.rglob('*'):
            if path.is_file():retain(path)
        return result
    try:
        seed=json.loads((ROOT/'.runtime/nemo-vm/inventory-data-seed.json').read_text());sql=validate_seed(ROOT,seed)
        with reserve_cluster(ROOT,slot=0) as cluster:
            cluster.start(sql);profile=cluster.profile.record();initial=data.initialize(ROOT,cluster)
            truth=data.validation.expected(initial['products'],source_binding=data.validation.source_identity())
            directory=cluster.profile.base/'inventory-evaluator-data';directory.mkdir()
            async def collect():
                async with woocommerce_session(ROOT,cluster,directory) as (client,child):
                    async def call(name,args):return (await client.call_tool(name,args)).model_dump(mode='json')
                    return await data.collect_products(call,initial['products'])
            products,pages=asyncio.run(collect())
            pages_path=directory/'original-pages.json';save(pages_path,pages);retain(pages_path)
            child_path=directory/'woocommerce-session.json';retain(child_path)
            proof={'schema_version':'inventory-evaluation-source-v1','task_id':data.TASK_ID,'profile':profile,
                'contract_source':data.validation.source_identity(),'readonly_sha256':initial['readonly_sha256'],
                'pages_ref':pages_path.relative_to(ROOT).as_posix(),'pages_sha256':hashlib.sha256(pages_path.read_bytes()).hexdigest()}
            proof_path=cluster.profile.base/evaluation.SOURCE_NAME;save(proof_path,proof);retain(proof_path)
            check(0,seed['runtime_activation'] is False and evaluation.REFERENCE_MODE=='project_derived_sql_oracle')
            check(1,data.catalog_projection(products)==data.catalog_projection(initial['products']))
            for event in truth['events']:
                execute(cluster,'INSERT INTO gcal.events(id,summary,description,start_datetime,end_datetime,start_timezone,end_timezone) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                    ('fixture-'+str(len(execute(cluster,'SELECT id FROM gcal.events'))),event['summary'],event['description'],event['start_datetime'],event['end_datetime'],event['start_timezone'],event['end_timezone']))
            folder=execute(cluster,"INSERT INTO email.folders(name) VALUES('Sent') RETURNING id")[0][0]
            mid=execute(cluster,'INSERT INTO email.messages(folder_id,from_addr,to_addr,subject,body_text) VALUES(%s,%s,%s::jsonb,%s,%s) RETURNING id',
                (folder,data.validation.SENDER,json.dumps([data.validation.RECIPIENT]),data.validation.SUBJECT,truth['email_body']))[0][0]
            execute(cluster,'INSERT INTO email.sent_log(message_id) VALUES(%s)',(mid,))
            artifacts.write_report(ROOT,cluster,truth['report_text'])
            result=measure(CASES[0],cluster);check(2,result['component_measurement_passed'] is True)
            wrong=truth['report_text'].replace(truth['products'][0]['name'],truth['products'][0]['name']+' WRONG',1)
            artifacts.write_report(ROOT,cluster,wrong)
            result=measure(CASES[1],cluster);check(3,result['original_passed'] is True and result['supplemental']['supplemental_passed'] is False)
            artifacts.write_report(ROOT,cluster,truth['report_text'])
            execute(cluster,"UPDATE gcal.events SET start_datetime='2026-03-10T13:30:00Z',end_datetime='2026-03-10T15:00:00Z',start_timezone='UTC',end_timezone='UTC' WHERE id='fixture-0'")
            result=measure(CASES[2],cluster);check(4,result['original_passed'] is True and result['supplemental']['supplemental_passed'] is False)
            execute(cluster,"UPDATE gcal.events SET start_datetime=%s,end_datetime=%s,start_timezone=%s,end_timezone=%s WHERE id='fixture-0'",
                (data.validation.START.isoformat(),data.validation.END.isoformat(),data.validation.ZONE,data.validation.ZONE))
            execute(cluster,"INSERT INTO gcal.events(id,summary,start_datetime,end_datetime) VALUES('extra-sql-fixture','Unrelated SQL fixture',%s,%s)",
                (data.validation.START.isoformat(),data.validation.END.isoformat()))
            result=measure(CASES[3],cluster);check(5,result['original_passed'] is True and result['supplemental']['supplemental_passed'] is False)
            execute(cluster,"DELETE FROM gcal.events WHERE id='extra-sql-fixture'")
            execute(cluster,'UPDATE email.messages SET to_addr=%s::jsonb WHERE id=%s',(json.dumps(['wrong@example.org']),mid))
            result=measure(CASES[4],cluster);check(6,result['original_passed'] is False and result['component_measurement_passed'] is False)
            execute(cluster,'UPDATE email.messages SET to_addr=%s::jsonb WHERE id=%s',(json.dumps([data.validation.RECIPIENT]),mid))
            execute(cluster,'DELETE FROM email.sent_log WHERE message_id=%s',(mid,))
            result=measure(CASES[5],cluster);check(7,result['original_passed'] is True and result['supplemental']['supplemental_passed'] is False)
            execute(cluster,'INSERT INTO email.sent_log(message_id) VALUES(%s)',(mid,))
            result=measure(CASES[6],cluster);check(8,result['component_measurement_passed'] is True)
            retain(cluster.profile.base/'cluster_owner.json');retain(cluster.profile.base/'inventory-initialized.json')
            cluster.stop()
            result=measure(CASES[7],cluster);check(9,result['component_measurement_passed'] is None and result['original_evaluator_executed'] is False)
        stopped=not cluster.started and not (cluster.profile.data/'postmaster.pid').exists()
        after=shared_state(ROOT);ledger_after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ledgers}
        check(10,before==after and ledger_before==ledger_after)
        check(11,stopped and json.loads(child_path.read_text())['closed'])
    except Exception as exc:
        error=type(exc).__name__+': '+str(exc);after=shared_state(ROOT);ledger_after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ledgers}
    receipt={'schema_version':'inventory-evaluator-components-v1','at':datetime.now(timezone.utc).isoformat(),
        'passed':error is None and len(checks)==len(CHECKS),'error':error,'checks':checks,'measurements':measurements,
        'profile':profile,'retained_sha256':retained,'child_session_ref':child_path.relative_to(ROOT).as_posix() if child_path else None,
        'shared_before':before,'shared_after':after,'ledgers_before':ledger_before,'ledgers_after':ledger_after,'pg_stopped':stopped,
        'fixture_mode':'explicit_sql_not_original_calendar_or_email_tools','original_product_tool_calls':len(pages) if child_path else 0,
        'nemo_task_executed':False,'runtime_activation':False,'training_admission':False,'email_tools_called':False,'calendar_tools_called':False,
        'reference_files_read':False,'official_benchmark_score_available':False,
        'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in SOURCES}}
    save(output/'result.json',receipt)
    if error:print(json.dumps({'passed':False,'error':error,'receipt':str(output/'result.json')}));return 1
    print(json.dumps({'passed':True,'checks':len(checks),'receipt':str(output/'result.json'),'audit':audit(ROOT,output/'result.json')}));return 0


if __name__=='__main__':raise SystemExit(main())
