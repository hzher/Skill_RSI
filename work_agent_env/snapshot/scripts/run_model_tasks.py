"""Run bounded real-model tasks through the owned NeMo runtime; preserve every attempt."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
# Legacy operator helpers use sibling imports; support both CLI and Python-library entry.
sys.path.insert(0,str(ROOT/'scripts'))
from integrations.model_endpoint import load_private_environment, load_settings
from integrations.model_task_executor import execute_task
from integrations.runtime_identity import classify, expected_identity
from scripts.mainline_runtime import bridge_request
from workenv.leases import owned_lock


def preflight():
    from jsonschema import Draft202012Validator
    status=bridge_request('/status')
    with urllib.request.urlopen('http://127.0.0.1:18700/provenance',timeout=10) as stream: provenance=json.load(stream)
    identity=classify(provenance,status,expected_identity(ROOT),'98de01c6168062fb746d45f8cd56b38b5580555b')
    if not identity['ready'] or identity['active_session']:
        raise RuntimeError('Runtime identity mismatch or an existing task is active; no task started')
    return {'identity':identity,'resource_slots':status.get('isolated',{}).get('resource_slots')}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',choices=('openai','claude'),action='append',required=True)
    parser.add_argument('--task',action='append')
    parser.add_argument('--all-adapted',action='store_true')
    parser.add_argument('--private-env',type=Path,required=True)
    parser.add_argument('--max-steps',type=int,default=100)
    parser.add_argument('--wall-seconds',type=int,default=1800)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--skip-passed',action='store_true')
    args=parser.parse_args(argv)
    if not 1<=args.max_steps<=100 or not 30<=args.wall_seconds<=1800: parser.error('Task budget exceeds supported limits')
    rows=json.loads((ROOT/'configs/task_adapter_profiles.json').read_text())['profiles'].values()
    adapted=sorted({row['task_id'] for row in rows if 'isolated' in row['resource_backends']})
    tasks=adapted if args.all_adapted else list(dict.fromkeys(args.task or []))
    if not tasks or any(task not in adapted for task in tasks): parser.error('Select known isolated tasks or --all-adapted')
    env=load_private_environment(ROOT,args.private_env)
    settings={name:load_settings(ROOT/'configs/model_endpoints.example.json',name,env) for name in dict.fromkeys(args.profile)}
    if not args.execute:
        print(json.dumps({'status':'configured_not_executed','tasks':tasks,'models':{k:v.model for k,v in settings.items()},'max_steps':args.max_steps,'wall_seconds':args.wall_seconds}))
        return 0
    public=ROOT/'evidence/model_tasks'
    public.mkdir(parents=True,exist_ok=True)
    results=[]
    with owned_lock(ROOT/'.runtime/model-task-campaign.lock',ROOT,'nemo_real_model_tasks'):
        for task in tasks:
            for profile,cfg in settings.items():
                if args.skip_passed:
                    previous=[json.loads(p.read_text()) for p in public.glob('*.json')]
                    if any(r['task_id']==task and r['requested_model']==cfg.model and r['status']=='passed' and r.get('closed') is True
                           and r.get('protocol')==cfg.protocol and r.get('max_output_tokens')==cfg.max_tokens
                           and r.get('reasoning_effort')==cfg.reasoning_effort and r.get('max_steps')==args.max_steps
                           and r.get('wall_seconds')==args.wall_seconds
                           and r.get('endpoint_sha256')==hashlib.sha256(cfg.base_url.encode()).hexdigest()
                           and r.get('environment_source_sha256')==expected_identity(ROOT)
                           and all((ROOT/name).is_file() and hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==value for name,value in r['source_sha256'].items()) for r in previous):
                        print(json.dumps({'task':task,'model':cfg.model,'status':'already_passed_current_executor'}),flush=True)
                        continue
                observed=preflight()
                result=execute_task(ROOT,task,cfg,max_steps=args.max_steps,wall_seconds=args.wall_seconds)
                result['runtime_preflight']=observed
                result['profile']=profile
                (public/(result['run_id']+'.json')).write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
                results.append(result)
                print(json.dumps({k:result[k] for k in ('run_id','task_id','requested_model','status','failure_kind','closed','model_http_requests','elapsed_seconds')}),flush=True)
                if not result['closed']: return 2
    return 0 if all(result['status']=='passed' for result in results) else 1


if __name__=='__main__': raise SystemExit(main())
