"""Recompute inventory data-component evidence, without live task activation."""
import ast
import hashlib
import json
from pathlib import Path

from integrations import inventory_data as data
from integrations.mainline_audit import read_json, source_binding
from integrations.episode_cluster import EpisodeProfile
from scripts.prepare_inventory_data_seed import validate_seed
from training.inventory_task_review import public_blob
from workenv.core import contained

CHECKS=('source_bound_seed_and_empty_writable_schemas','two_owned_isolated_pg_profiles',
 'original_preprocess_and_inventory_identity','complete_original_product_pages_match_sql',
 'independent_sql_matches_python_zero_stock','public_evaluator_constants_compatibility_only',
 'owned_utf8_reports_match_complete_products','wrong_report_is_measurable_failure',
 'interrupted_report_write_not_replayed','database_reset_preserves_sibling',
 'all_readonly_wc_tables_unchanged','shared_database_and_nemo_ledgers_unchanged',
 'original_woocommerce_children_closed','owned_pg_slots_stopped')
SOURCES=('integrations/inventory_data.py','integrations/inventory_data_audit.py','scripts/prepare_inventory_data_seed.py',
 'scripts/verify_inventory_data.py','adapters/inventory_alert_validation.py','adapters/inventory_alert_artifacts.py',
 'integrations/episode_cluster.py','integrations/order_analysis_resource.py','adapters/woocommerce_native.mjs',
 'workenv/atomic_io.py','workenv/process_identity.py','training/inventory_task_review.py')


def constant_ids(root):
    raw,_=public_blob(root,'evaluation/main.py')
    tree=ast.parse(raw)
    value=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
        and any(isinstance(t,ast.Name) and t.id=='OOS_PRODUCT_IDS' for t in node.targets))
    if not isinstance(value,list) or any(type(n) is not int for n in value):raise ValueError('Public evaluator constant shape differs')
    return value


def audit(root,path):
    root=Path(root).resolve();receipt=read_json(path,root)
    if (receipt.get('schema_version')!='inventory-data-components-v1' or receipt.get('passed') is not True
        or receipt.get('task_id')!=data.TASK_ID or receipt.get('task_passed') is not None or receipt.get('reward') is not None
        or any(type(receipt.get(k)) is not int or receipt[k]!=0 for k in ('real_model_calls','training_updates'))
        or [c.get('name') for c in receipt.get('checks',[])]!=list(CHECKS)
        or any(c.get('passed') is not True for c in receipt['checks'])
        or any(receipt.get(k) is not False for k in ('nemo_task_executed','runtime_activation','training_admission',
            'email_tools_called','calendar_tools_called','original_evaluator_executed','reference_files_read','official_benchmark_score_available'))):
        raise ValueError('Inventory data receipt scope or checks differ')
    source_binding(root,receipt.get('source_sha256'))
    if set(receipt['source_sha256'])!=set(SOURCES):raise ValueError('Inventory data source coverage differs')
    retained=receipt.get('retained_sha256',{})
    if not 20<=len(retained)<=100:raise ValueError('Inventory retained evidence budget differs')
    for name,digest in retained.items():
        if not name.startswith(('.runtime/native-episodes/','evidence/mainline/inventory-data-')):
            raise ValueError('Inventory retained path outside data-component evidence')
        p=contained(root,name);read_json(p,root,limit=8*1024*1024) if p.suffix=='.json' else None
        from integrations.mainline_audit import track_input
        if hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise ValueError('Inventory retained bytes changed')
        track_input(p,root,digest)
    def load(name):
        if name not in retained:raise ValueError('Inventory evidence member not retained')
        return read_json(contained(root,name),root,limit=8*1024*1024)
    seed=load(receipt['seed_record_ref']);sql=validate_seed(root,seed)
    if sql.relative_to(root).as_posix() not in retained:raise ValueError('Seed SQL not retained')
    profiles=[EpisodeProfile(root,p['resource_id'],p['slot']) for p in receipt['profiles']]
    if len(profiles)!=2 or {p.slot for p in profiles}!={0,1} or profiles[0].resource_id==profiles[1].resource_id:
        raise ValueError('Inventory resources not independent')
    total_pages=0
    for index,profile in enumerate(profiles):
        profile.validate_record(receipt['profiles'][index])
        initial=load(receipt['initial_snapshot_refs'][index]);truth=data.validate_snapshot(initial)
        if initial['readonly_tables']!={k:v for k,v in seed['source_state'].items() if k.startswith('wc.')}:
            raise ValueError('Restored inventory wc data differs from seed source')
        if initial['events'] or any(initial['email'][n] for n in data.RESET_TABLES):raise ValueError('Initial writable rows not empty')
        observed=load(receipt['after_paging_refs'][index]);data.validate_snapshot(observed)
        if observed!=initial:raise ValueError('Original read modified owned SQL state')
        page_result=data.audit_pages(load(receipt['page_journal_refs'][index]),initial['products']);total_pages+=page_result['pages']
        report=contained(root,receipt['report_refs'][index]);raw=report.read_bytes()
        if report.relative_to(root).as_posix() not in retained:raise ValueError('Final owned report not retained')
        data.validation.check_report(raw.decode('utf-8'),truth)
        marker=load((profile.base/'inventory-initialized.json').relative_to(root).as_posix())
        if marker.get('task_id')!=data.TASK_ID or marker.get('profile')!=profile.record() or marker.get('state')!='ready' or marker.get('contract_source')!=data.validation.source_identity():
            raise ValueError('Inventory initialization marker rebound')
        run=contained(root,marker['preprocess_ref'])
        original,_=public_blob(root,'preprocess/main.py')
        if (run/'original_preprocess.py').relative_to(root).as_posix() not in retained or (run/'original_preprocess.py').read_bytes()!=original:
            raise ValueError('Inventory original preprocess source differs')
        if load((run/'command.json').relative_to(root).as_posix())['exit_code']!=0:raise ValueError('Original preprocess failed')
        child=load(receipt['child_session_refs'][index]);before,after=child['before'],child['after']
        if (child.get('closed') is not True or child.get('profile')!=profile.record() or before.get('status')!='alive'
            or not before.get('creation_id') or after.get('pid')!=before.get('pid')
            or not (after.get('status') in ('not_found','exited') or after.get('status')=='alive' and after.get('creation_id') and after['creation_id']!=before['creation_id'])):
            raise ValueError('Original WooCommerce child closure unknown')
    before=load(receipt['before_reset_ref']);after=load(receipt['after_reset_ref'])
    data.validate_snapshot(before);data.validate_snapshot(after)
    if len(before['events'])!=1 or after['events'] or before['products']!=after['products'] or before['readonly_sha256']!=after['readonly_sha256']:
        raise ValueError('Original inventory database reset not proven')
    if load(receipt['sibling_before_ref'])!=load(receipt['sibling_after_ref']):raise ValueError('Sibling resource changed during reset')
    for observed in receipt['closed_profiles']:
        if observed.get('started') is not False or observed.get('postmaster_pid_present') is not False:raise ValueError('Inventory PG not closed')
    if len(receipt['closed_profiles'])!=2 or receipt['shared_before']!=receipt['shared_after'] or receipt['ledgers_before']!=receipt['ledgers_after']:
        raise ValueError('Inventory shared resources or NeMo ledgers changed')
    for entry in receipt['closed_profiles']:
        profile=profiles[entry['slot']]
        if entry.get('profile')!=profile.record():raise ValueError('Closed inventory profile rebound')
        from integrations.mainline_audit import track_absence
        track_absence(profile.data/'postmaster.pid',root)
    fault=load(receipt['file_fault_ref']);original=load(fault['original_intent_ref'])
    resolved=load(fault['intent_ref'])
    if (original.get('state')!='prepared' or resolved.get('state')!='aborted' or fault.get('decision')!='not_committed'
        or fault.get('replay_performed') is not False or receipt.get('write_retry_blocked') is not True):
        raise ValueError('Inventory interrupted write was not safely reconciled')
    if original.get('profile')!=profiles[0].record() or original.get('artifact_ref')!=receipt['report_refs'][0]:
        raise ValueError('Interrupted inventory file intent rebound')
    if original.get('previous_sha256')!=hashlib.sha256(contained(root,receipt['report_refs'][0]).read_bytes()).hexdigest():
        raise ValueError('Inventory reconciliation did not preserve original report bytes')
    ids=[r['id'] for r in load(receipt['initial_snapshot_refs'][0])['sql_zero_stock']]
    if receipt['public_evaluator_constants_match_sql']!=(constant_ids(root)==ids):raise ValueError('Evaluator constant compatibility claim differs')
    negative=contained(root,receipt['wrong_report_ref'])
    if negative.relative_to(root).as_posix() not in retained:raise ValueError('Wrong report negative bytes missing')
    try:data.validation.check_report(negative.read_text(encoding='utf-8'),data.validate_snapshot(load(receipt['initial_snapshot_refs'][0])))
    except data.validation.InvalidReport:pass
    else:raise ValueError('Wrong report actually passes the supplemental contract')
    if receipt.get('wrong_report_rejected') is not True:raise ValueError('Wrong report negative measurement missing')
    return {'owned_resources':2,'products':len(initial['products']),'zero_stock_products':len(truth['products']),
        'original_product_pages':total_pages,'original_preprocess_executed':True,'original_evaluator_executed':False,
        'email_tools_called':False,'calendar_tools_called':False,'new_runtime_profiles':0,'training_admission':False}
