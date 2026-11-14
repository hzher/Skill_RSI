"""Read-only caller handshake. No model keys, task submission or resource initialization."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from integrations.skill_rsi_client import RSIClientError,SkillRSIClient
from integrations.model_endpoint import EndpointError,strict_json
from integrations.skill_rsi_contract import MAX_REQUEST_BYTES,RSIError,restrict_tools,skill_version,validate_request
from training.skill_contract import canonical


def _private_bytes(path,limit,code):
    path=Path(path).absolute()
    try:
        if any(item.is_symlink() or item.is_junction() for item in (path,*path.parents)):
            raise ValueError()
        if not path.is_file() or path.stat().st_size>limit:
            raise ValueError()
        if os.name!='nt' and path.stat().st_mode & 0o077:
            raise ValueError()
        with path.open('rb') as stream:raw=stream.read(limit+1)
        if len(raw)>limit:raise ValueError()
        return raw
    except (OSError,ValueError,UnicodeError):
        raise RSIClientError(code) from None


def load_token(path):
    try:
        token=_private_bytes(path,256,'handoff_token_file_unavailable').decode('utf-8').strip()
        if not 32<=len(token)<=256 or any(not 33<=ord(c)<=126 for c in token):raise ValueError()
        return token
    except (ValueError,UnicodeError):
        raise RSIClientError('handoff_token_file_unavailable') from None


def load_request(path):
    raw=_private_bytes(path,MAX_REQUEST_BYTES,'candidate_request_file_unavailable')
    try:return strict_json(raw)
    except EndpointError:raise RSIClientError('candidate_request_json_invalid') from None


def _capability(client):
    capability=client.capabilities()
    descriptor=capability.get('executor')
    if not isinstance(descriptor,dict) or hashlib.sha256(canonical(descriptor)).hexdigest()!=client.executor_id:
        raise RSIClientError('handoff_executor_descriptor_mismatch')
    available=capability.get('tasks')
    environment=descriptor.get('environment_source_sha256')
    if (not isinstance(available,list) or not available or any(not isinstance(task,str) for task in available)
        or not isinstance(environment,dict) or not environment):
        raise RSIClientError('handoff_capability_invalid')
    checks={'skill_v1_supported':capability.get('skill_schema')=='skill-v1',
        'executor_preconditions_supported':capability.get('executor_precondition_supported') is True,
        'automatic_replay_disabled':capability.get('automatic_replay') is False,
        'single_active_evaluation':type(capability.get('max_active_evaluations')) is int
                                   and capability['max_active_evaluations']==1}
    return capability,environment,checks


def _catalog(client,task,environment):
    catalog=client.tool_catalog(task)
    run=catalog.get('source_run_id')
    if (catalog.get('environment_source_sha256')!=environment
        or catalog.get('source_kind')!='retained_closed_discovery'
        or catalog.get('live_availability_attested') is not False
        or not isinstance(run,str) or not re.fullmatch('[a-f0-9]{32}',run)
        or not isinstance(catalog.get('tools'),list)):
        raise RSIClientError('handoff_catalog_binding_invalid')
    return catalog


def check_handoff(client, tasks=()):
    capability,environment,checks=_capability(client)
    available=capability['tasks']
    rows=[]
    for index,task in enumerate(dict.fromkeys(tasks)):
        if task not in available:
            rows.append({'task_index':index,'passed':False,'error':'task_not_available'})
            continue
        try:
            catalog=_catalog(client,task,environment)
            rows.append({'task_id':task,'passed':True,'tool_count':len(catalog['tools']),'source_run_id':catalog['source_run_id']})
        except RSIClientError as error:
            rows.append({'task_id':task,'passed':False,'error':error.code,'http_status':error.http_status})
    return {'schema_version':'skill-rsi-handoff-check-v1','passed':all(checks.values()) and all(row['passed'] for row in rows),
        'checks':checks,'executor_id':client.executor_id,'available_task_count':len(available),
        'environment_fingerprint_sha256':hashlib.sha256(canonical(environment)).hexdigest(),
        'catalogs':rows,'runtime_availability_attested':False,'model_http_requests':0,
        'evaluation_submitted':False,'training_updates':0,'scope':'caller_get_only_handshake_not_runtime_or_task_acceptance'}


def check_candidate(client,request):
    # Freeze the existing evaluation contract before the first capability RPC.
    try:body=validate_request(request,[request.get('task_id')] if isinstance(request,dict) else [])
    except RSIError as error:raise RSIClientError(error.code) from None
    capability,environment,checks=_capability(client)
    if not all(checks.values()):raise RSIClientError('handoff_capability_invalid')
    if body['task_id'] not in capability['tasks']:raise RSIClientError('task_not_available')
    catalog=_catalog(client,body['task_id'],environment)
    tools=catalog['tools']
    if (len(tools)>4096 or any(not isinstance(tool,dict) or not isinstance(tool.get('name'),str)
        or not tool['name'] for tool in tools)):
        raise RSIClientError('candidate_catalog_invalid')
    mapping={tool['name']:None for tool in tools}
    if len(mapping)!=len(tools):raise RSIClientError('candidate_catalog_invalid')
    skill=body['skill'];error=None
    unknown=len(set(skill['allowed_tools'])-set(mapping)) if skill is not None else 0
    if skill is not None:
        try:restrict_tools(tools,mapping,skill)
        except RSIError as rejected:error=rejected.code
    return {'schema_version':'skill-rsi-candidate-check-v1','passed':error is None,'error':error,
        'request_id':body['request_id'],'task_id':body['task_id'],'executor_id':client.executor_id,
        'skill_sha256':skill_version(skill) if skill is not None else None,
        'skill_mode':'external_skill' if skill is not None else 'no_skill_baseline',
        'tool_permissions_checked':skill is not None,
        'allowed_tool_count':len(skill['allowed_tools']) if skill is not None else None,
        'unknown_tool_count':unknown,'source_run_id':catalog['source_run_id'],
        'runtime_availability_attested':False,'business_success_attested':False,'training_eligible':False,
        'model_http_requests':0,'evaluation_submitted':False,'training_updates':0,
        'scope':'local_request_and_retained_catalog_contract_only'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file',type=Path,required=True)
    parser.add_argument('--base-url',default='http://127.0.0.1:18820')
    parser.add_argument('--task',action='append')
    parser.add_argument('--request-file',type=Path,help='Check a private evaluation request using GET only; do not combine with --task')
    parser.add_argument('--timeout-seconds',type=float,default=10)
    args=parser.parse_args(argv)
    try:
        if args.request_file and args.task:raise RSIClientError('candidate_check_arguments_conflict')
        client=SkillRSIClient(load_token(args.token_file),base_url=args.base_url,timeout_seconds=args.timeout_seconds)
        result=check_candidate(client,load_request(args.request_file)) if args.request_file else check_handoff(client,args.task or ())
    except RSIClientError as error:
        result={'schema_version':'skill-rsi-candidate-check-v1' if args.request_file else 'skill-rsi-handoff-check-v1',
            'passed':False,'error':error.code,
            'http_status':error.http_status,'model_http_requests':0,'evaluation_submitted':False,
            'runtime_availability_attested':False,'training_updates':0}
    print(json.dumps(result,ensure_ascii=True,allow_nan=False))
    return 0 if result['passed'] else 2


if __name__=='__main__':raise SystemExit(main())
