"""Real owned product/SQL/init/text component diagnostic; no seventh task."""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from adapters import inventory_alert_artifacts as artifacts
from integrations import inventory_data as data
from integrations.inventory_data_audit import CHECKS, SOURCES, constant_ids, audit
from integrations.episode_cluster import reserve_cluster, require_dependents_closed
from integrations.order_analysis_resource import woocommerce_session
from scripts.prepare_inventory_data_seed import shared_state, validate_seed
from workenv.core import ResourceError, contained, save


def main():
    output=ROOT/'evidence/mainline'/('inventory-data-'+uuid4().hex);output.mkdir(parents=True)
    checks=[];retained={};clusters=[];profiles=[];initials=[];paging=[];journals=[];children=[];reports=[];closed=[]
    def retain(path):retained[path.relative_to(ROOT).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    def store(name,value):
        path=output/name;save(path,value);retain(path);return path.relative_to(ROOT).as_posix()
    def check(name,passed):
        checks.append({'name':name,'passed':bool(passed)})
        if not passed:raise AssertionError(name)
    shared_before=shared_state(ROOT)
    ledger_paths=[ROOT/'.runtime/nemo-vm'/n for n in ('resource-sessions.json','isolated-sessions.json','synthetic-sessions.json')]
    ledgers_before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ledger_paths}
    seed=json.loads((ROOT/'.runtime/nemo-vm/inventory-data-seed.json').read_text());sql=validate_seed(ROOT,seed)
    retain(sql);seed_ref=store('seed.json',seed)
    fault_ref=before_reset_ref=after_reset_ref=sibling_before_ref=sibling_after_ref=wrong_ref=None
    wrong_rejected=retry_blocked=False;constants_match=False;error=None
    async def run():
        nonlocal fault_ref,before_reset_ref,after_reset_ref,sibling_before_ref,sibling_after_ref,wrong_ref,wrong_rejected,retry_blocked,constants_match
        check(CHECKS[0],seed['schemas']==list(data.SCHEMAS) and seed['writable_rows_included'] is False)
        with ExitStack() as owned:
            for slot in (0,1):
                cluster=owned.enter_context(reserve_cluster(ROOT,slot=slot));clusters.append(cluster)
                cluster.start(sql);profiles.append(cluster.profile.record())
            check(CHECKS[1],profiles[0]['resource_id']!=profiles[1]['resource_id'] and profiles[0]['data_directory']!=profiles[1]['data_directory'])
            states=[data.initialize(ROOT,c) for c in clusters]
            for i,state in enumerate(states):initials.append(store('initial-'+str(i)+'.json',state))
            check(CHECKS[2],all(not s['events'] and not any(s['email'][n] for n in data.RESET_TABLES) for s in states))
            page_count=0;truths=[]
            for index,cluster in enumerate(clusters):
                directory=cluster.profile.base/'inventory-products-run';directory.mkdir()
                async with woocommerce_session(ROOT,cluster,directory) as (client,child):
                    schemas=[t.model_dump(mode='json') for t in (await client.list_tools()).tools]
                    if not any(t['name']=='woo_products_list' for t in schemas):raise ResourceError('Original product method missing')
                    save(directory/'tool_schemas.json',schemas);retain(directory/'tool_schemas.json')
                    async def call(name,args):return (await client.call_tool(name,args)).model_dump(mode='json')
                    products,journal=await data.collect_products(call,states[index]['products'])
                session=directory/'woocommerce-session.json';retain(session);children.append(session.relative_to(ROOT).as_posix())
                after=data.snapshot(ROOT,cluster);paging.append(store('after-paging-'+str(index)+'.json',after))
                journals.append(store('original-pages-'+str(index)+'.json',journal));page_count+=len(journal)
                if after!=states[index]:raise ResourceError('Original product read changed SQL state')
                truths.append(data.validation.expected(products,source_binding=data.validation.source_identity()))
                artifacts.write_report(ROOT,cluster,truths[-1]['report_text'])
                reports.append((cluster.profile.base/'inventory-workspace'/data.validation.FILENAME).relative_to(ROOT).as_posix())
            check(CHECKS[3],page_count>=2 and all(data.audit_pages(json.loads(contained(ROOT,r).read_text()),s['products']) for r,s in zip(journals,states)))
            check(CHECKS[4],all(data.validate_snapshot(s)['products']==s['sql_zero_stock'] for s in states))
            constants_match=constant_ids(ROOT)==[r['id'] for r in states[0]['sql_zero_stock']]
            check(CHECKS[5],constants_match)
            check(CHECKS[6],all(artifacts.read_report(ROOT,c)['text']==truths[i]['report_text'] for i,c in enumerate(clusters)))
            wrong=output/'wrong-report.txt';wrong.write_text(truths[0]['report_text'].replace(' - ',' - WRONG - ',1),encoding='utf-8');retain(wrong);wrong_ref=wrong.relative_to(ROOT).as_posix()
            try:data.validation.check_report(wrong.read_text(encoding='utf-8'),truths[0])
            except data.validation.InvalidReport:wrong_rejected=True
            check(CHECKS[7],wrong_rejected)
            with patch.object(artifacts,'replace_atomic',side_effect=PermissionError('injected local text replacement failure')):
                try:artifacts.write_report(ROOT,clusters[0],'uncommitted diagnostic replacement')
                except PermissionError:pass
                else:raise AssertionError('Expected injected replacement failure')
            try:artifacts.write_report(ROOT,clusters[0],'retry must be refused')
            except ResourceError:retry_blocked=True
            pending=next(p for p in clusters[0].profile.base.glob(artifacts.PREFIX+'*.json') if json.loads(p.read_text())['state']=='prepared')
            resolution=artifacts.reconcile_write(ROOT,clusters[0],pending.name)
            fault_ref=store('file-fault.json',{**resolution,'intent_ref':pending.relative_to(ROOT).as_posix()})
            check(CHECKS[8],retry_blocked and resolution['decision']=='not_committed' and resolution['replay_performed'] is False)
            # Explicit SQL diagnostic sentinel, not a Calendar tool invocation.
            conn=clusters[0].connect()
            try:
                with conn,conn.cursor() as cursor:
                    cursor.execute("INSERT INTO gcal.events(id,summary,start_datetime,end_datetime) VALUES(%s,%s,%s,%s)",
                        ('inventory-diagnostic-'+uuid4().hex,'SQL reset sentinel','2026-03-10T13:00:00Z','2026-03-10T14:00:00Z'))
            finally:conn.close()
            before_reset_ref=store('before-reset.json',data.snapshot(ROOT,clusters[0]))
            sibling_before_ref=store('sibling-before-reset.json',data.snapshot(ROOT,clusters[1]))
            after=data.initialize(ROOT,clusters[0],resetting=True)
            after_reset_ref=store('after-reset.json',after)
            sibling_after=data.snapshot(ROOT,clusters[1]);sibling_after_ref=store('sibling-after-reset.json',sibling_after)
            check(CHECKS[9],not after['events'] and sibling_after==states[1])
            check(CHECKS[10],all(s['readonly_tables']==after['readonly_tables'] for s in states))
            for cluster in clusters:
                profile=cluster.profile
                for path in (profile.base/'cluster_owner.json',profile.base/'inventory-initialized.json',
                             profile.base/'inventory-workspace'/data.validation.FILENAME):retain(path)
                for directory in profile.base.glob('inventory-preprocess-*'):
                    for name in ('original_preprocess.py','command.json','stdout.txt','stderr.txt'):retain(directory/name)
                for pattern in ('inventory-artifact-write-*.json','inventory-artifact-history/*.txt','inventory-artifact-intent-history/*.json'):
                    for path in profile.base.glob(pattern):retain(path)
        for cluster in clusters:
            closed.append({'slot':cluster.profile.slot,'profile':cluster.profile.record(),'started':cluster.started,
                'postmaster_pid_present':(cluster.profile.data/'postmaster.pid').exists()})
    try:
        asyncio.run(run())
        shared_after=shared_state(ROOT)
        ledgers_after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ledger_paths}
        check(CHECKS[11],shared_before==shared_after and ledgers_before==ledgers_after)
        check(CHECKS[12],all(json.loads(contained(ROOT,p).read_text())['closed'] for p in children))
        check(CHECKS[13],len(closed)==2 and all(c['started'] is False and c['postmaster_pid_present'] is False for c in closed))
    except Exception as exc:
        error=type(exc).__name__+': '+str(exc);shared_after=shared_state(ROOT)
        ledgers_after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ledger_paths}
    receipt={'schema_version':'inventory-data-components-v1','at':datetime.now(timezone.utc).isoformat(),'passed':error is None and len(checks)==len(CHECKS),
        'task_id':data.TASK_ID,'task_passed':None,'reward':None,'real_model_calls':0,'training_updates':0,'official_benchmark_score_available':False,
        'checks':checks,'error':error,'profiles':profiles,'retained_sha256':retained,'seed_record_ref':seed_ref,
        'initial_snapshot_refs':initials,'after_paging_refs':paging,'page_journal_refs':journals,'child_session_refs':children,'report_refs':reports,
        'before_reset_ref':before_reset_ref,'after_reset_ref':after_reset_ref,'sibling_before_ref':sibling_before_ref,'sibling_after_ref':sibling_after_ref,
        'file_fault_ref':fault_ref,'wrong_report_ref':wrong_ref,'wrong_report_rejected':wrong_rejected,'write_retry_blocked':retry_blocked,
        'public_evaluator_constants_match_sql':constants_match,'closed_profiles':closed,'shared_before':shared_before,'shared_after':shared_after,
        'ledgers_before':ledgers_before,'ledgers_after':ledgers_after,'nemo_task_executed':False,'runtime_activation':False,'training_admission':False,
        'email_tools_called':False,'calendar_tools_called':False,'original_evaluator_executed':False,'reference_files_read':False,
        'calendar_reset_sentinel':'explicit_sql_fixture_only','reset_scope':'original_database_preprocess_only_report_file_preserved',
        'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in SOURCES}}
    save(output/'result.json',receipt)
    if error:print(json.dumps({'passed':False,'error':error,'receipt':str(output/'result.json')}));return 1
    detail=audit(ROOT,output/'result.json')
    print(json.dumps({'passed':True,'checks':len(checks),'receipt':str(output/'result.json'),'audit':detail}))
    return 0


if __name__=='__main__':raise SystemExit(main())
