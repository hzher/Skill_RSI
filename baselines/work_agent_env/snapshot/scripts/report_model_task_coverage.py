"""Account for every Gym seed and every model attempt without reading answers."""
from collections import Counter
import argparse
import hashlib
import json
from pathlib import Path
import sys
import re

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from workenv.core import save


def diagnose(root):
    """Export only allowlisted failure metadata, never model/tool business data."""
    root=Path(root)
    rows=[]
    for path in sorted((root/'evidence/model_tasks').glob('*.json')):
        record=json.loads(path.read_text())
        identity=record['run_id']
        if not re.fullmatch(r'[0-9a-f]{32}',identity): raise ValueError('Invalid model run identity')
        directory=root/'.runtime/model-task-runs'/identity
        trace=directory/'trace.jsonl'
        row={key:record.get(key) for key in ('run_id','task_id','requested_model','status','failure_kind','closed','model_http_requests','model_http_error_status','measurement')}
        row['operator_interrupted']=record.get('operator_interrupted') is True
        row['operator_stop_reason']='switch_to_tested_repair_package' if record.get('operator_stop_reason')=='switch_to_tested_repair_package' else None
        row['receipt_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        row['private_trace_available']=trace.is_file()
        row['schema_rejections']=0
        counts=Counter()
        if trace.is_file():
            if trace.is_symlink() or trace.stat().st_size>128*1024*1024: raise ValueError('Private trace exceeds supported boundary')
            with trace.open(encoding='utf-8') as stream:
                for line in stream:
                    if len(line)>8*1024*1024: raise ValueError('Private trace frame exceeds boundary')
                    event=json.loads(line);value=event.get('value')
                    if event['kind']=='tool_intent' and isinstance(value,dict):
                        name=value.get('name')
                        if isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',name):
                            row['last_requested_tool']=name;counts[name]+=1
                    if event['kind']=='tool_validation_rejected':row['schema_rejections']+=1
                    if event['kind']=='nemo_error' and isinstance(value,dict):
                        status=value.get('status');route=value.get('route')
                        if type(status) is int and 100<=status<=599:row['nemo_http_status']=status
                        if route in ('/isolated/tool','/isolated/write_workbook','/isolated/verify','/isolated/seed_session','/isolated/close_session'):
                            row['nemo_error_route']=route
                        detail=value.get('response',{}).get('detail') if isinstance(value.get('response'),dict) else None
                        # Exact known contract error, not an arbitrary server message.
                        if detail=='VIP customer pagination scope differs':row['diagnosis']='published_customer_schema_exceeds_runtime_scope'
            row['private_trace_sha256']=hashlib.sha256(trace.read_bytes()).hexdigest()
        row['requested_tool_counts']=dict(counts)
        rows.append(row)
    return {'schema_version':'model-task-diagnostics-v1','scope':'transport_and_tool_metadata_not_official_score',
            'attempts':rows,'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'contains_prompts_or_business_rows':False}


def collect(root, *, native_preflight=None):
    root=Path(root)
    catalog=json.loads((root/'training/toolathlon_seed_catalog.json').read_text())
    capabilities=json.loads((root/'training/toolathlon_seed_capabilities.json').read_text())
    profiles=json.loads((root/'configs/task_adapter_profiles.json').read_text())['profiles']
    adapted={p['task_id'] for p in profiles.values() if 'isolated' in p['resource_backends']}
    known={'woocommerce','google_sheet','google_calendar','howtocook','emails'}
    fixed_artifact_declarations={'excel','filesystem'}
    records=[]
    for path in sorted((root/'evidence/model_tasks').glob('*.json')):
        value=json.loads(path.read_text())
        if value.get('schema_version')!='nemo-model-task-v1': raise ValueError('Unknown model task receipt')
        if value.get('training_updates')!=0 or value.get('official_score_available') is not False: raise ValueError('Unexpected training or official-score claim')
        records.append(value)
    tasks=[]
    for entry in catalog['directories']:
        task=entry['task_id']; meta=capabilities['tasks'][task]
        declared=meta.get('needed_mcp_servers',[])
        extra=sorted(set(declared)-known-fixed_artifact_declarations)
        reasons=[]
        native_blockers=[]
        if native_preflight is None: native_blockers.append('native_container_preflight_not_observed')
        else:
            if native_preflight.get('cli_available') is not True: native_blockers.append('docker_cli_unavailable')
            elif native_preflight.get('engine_available') is not True: native_blockers.append('docker_engine_unavailable')
            if native_preflight.get('requested_image_available') is not True: native_blockers.append('requested_image_not_available_or_not_verified')
        if task not in adapted: reasons.append('no_current_nemo_task_profile')
        if task not in adapted and extra: reasons.append('additional_mcp_services_not_adapted')
        if task not in adapted and meta.get('file_asset_count',0): reasons.append('task_file_assets_not_staged')
        attempts=[r for r in records if r['task_id']==task]
        latest={}
        for row in sorted(attempts,key=lambda r:r['started_at']):
            latest[row['requested_model']]={'run_id':row['run_id'],'status':row['status'],'failure_kind':row['failure_kind'],
                                          'closed':row['closed'],'model_http_requests':row['model_http_requests'],
                                          'operator_interrupted':row.get('operator_interrupted') is True}
        tasks.append({'task_id':task,'task_tree':entry['source_tree'],'config_sha256':meta['config_sha256'],
            'nemo_isolated_profile_available':task in adapted,'declared_mcp_servers':declared,
            'additional_services':extra,'file_assets':meta.get('file_asset_count',0),'not_run_reasons':reasons,
            'native_container_blockers':native_blockers,
            'native_local_tools_declared':meta.get('needed_local_tools',[]),
            'fixed_artifact_interface_is_not_general_filesystem_or_excel':True,
            'model_attempts':len(attempts),'latest_by_requested_model':latest,
            'model_execution_status':'attempted' if attempts else 'ready_not_attempted' if task in adapted else 'requires_adaptation'})
    counts=Counter(r['status'] for r in records)
    return {'schema_version':'model-task-coverage-v1','source':'toolathlon_gym','source_commit':catalog['commit'],
        'scope':'all_seed_prerequisites_and_model_attempts_not_training_or_official_benchmark',
        'summary':{'seed_tasks':len(tasks),'adapted_nemo_tasks':len(adapted),'model_attempts':len(records),
                   'attempts_by_status':dict(counts),'tasks_with_any_passed_model_attempt':len({r['task_id'] for r in records if r['status']=='passed' and r['closed']}),
                   'tasks_not_yet_attempted':sum(not row['model_attempts'] for row in tasks),
                   'model_http_requests':sum(r.get('model_http_requests',0) for r in records),
                   'operator_interrupted_attempts':sum(r.get('operator_interrupted') is True for r in records),
                   'unclosed_attempts':sum(r.get('closed') is not True for r in records)},
        'native_runtime_preflight':native_preflight,'tasks':tasks,
        'reference_answers_read':False,'verified_task_bodies_read':False,'training_updates':0,
        'source_sha256':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in (
            'scripts/report_model_task_coverage.py','training/toolathlon_seed_catalog.json','training/toolathlon_seed_capabilities.json','configs/task_adapter_profiles.json')}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--diagnostics',action='store_true',help='Also export allowlisted metadata from private traces on this machine')
    args=parser.parse_args()
    if args.diagnostics:save(ROOT/'evidence/mainline/model_task_diagnostics.json',diagnose(ROOT))
    preflight=ROOT/'.runtime/model-task-preflight.json'
    observed=json.loads(preflight.read_text()) if preflight.exists() else None
    report=collect(ROOT,native_preflight=observed.get('docker') if observed else None)
    save(ROOT/'evidence/mainline/model_task_coverage.json',report)
    print(json.dumps(report['summary']))


if __name__=='__main__': raise SystemExit(main())
