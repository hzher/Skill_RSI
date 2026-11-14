"""Read-only comparison of retained initial conditions, with explicit missing evidence."""
import hashlib
from pathlib import Path
import re

from integrations.model_endpoint import strict_json
from integrations.model_task_executor import tool_inventory
from integrations.skill_rsi_audit import audit_comparison,audit_run,request_id
from integrations.skill_rsi_contract import RSIError
from integrations.episode_cluster import EpisodeProfile
from training.skill_contract import canonical
from workenv.core import no_links

SOURCES=('integrations/skill_rsi_conditions.py','scripts/check_skill_rsi_conditions.py',
         'integrations/skill_rsi_audit.py','integrations/model_task_executor.py',
         'integrations/episode_cluster.py','integrations/skill_rsi_contract.py')
TABLES={'wc.orders','gsheet.spreadsheets','gsheet.sheets','gsheet.cells','gsheet.permissions'}
SHA=re.compile('[a-f0-9]{64}')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class Inputs:
    def __init__(self,root):self.root,self.hashes=root,{}

    def raw(self,path,maximum=2*1024*1024):
        no_links(path,self.root)
        if not path.is_file() or path.stat().st_size>maximum:
            raise RSIError('conditions_input_unavailable')
        with path.open('rb') as stream:raw=stream.read(maximum+1)
        if len(raw)>maximum:raise RSIError('conditions_input_size_limit')
        sha=hashlib.sha256(raw).hexdigest()
        if path in self.hashes and self.hashes[path]!=sha:raise RSIError('conditions_input_changed')
        self.hashes[path]=sha
        return raw

    def json(self,path):return strict_json(self.raw(path))

    def unchanged(self):
        for path,sha in list(self.hashes.items()):
            if hashlib.sha256(self.raw(path,128*1024*1024)).hexdigest()!=sha:
                raise RSIError('conditions_input_changed')


def _one(root,job,inputs):
    result=job['result'];identity=request_id(result['request_id'])
    if not audit_run(root,root/'evidence/skill_rsi/runs'/(identity+'.json'))['passed']:
        raise RSIError('conditions_run_audit_failed')
    directory=root/'.runtime/model-task-runs'/result['run_id']
    report=inputs.json(directory/'result.json');manifest=inputs.json(directory/'manifest.json')
    if report['prompt_identity']!=manifest['prompt_identity']:
        raise RSIError('conditions_prompt_binding_invalid')
    prompt=report['prompt_identity']
    if (not isinstance(prompt,dict) or not isinstance(prompt.get('sha256'),str)
        or not SHA.fullmatch(prompt['sha256']) or prompt.get('path')!='tasks/finalpool/'+result['task_id']+'/docs/task.md'):
        raise RSIError('conditions_prompt_binding_invalid')
    sid=request_id(report['resources_session_id'])
    bound={'resources_session_id':sid,'episode_id':report['episode_id']}
    base=root/'.runtime/native-episodes'/sid
    profile=inputs.json(base/'cluster_owner.json')
    EpisodeProfile(root,sid,profile['slot']).validate_record(profile)
    ledger=inputs.json(base/'resources-session.json')
    if ledger.get('owner')!=str(root) or ledger.get('schema_version')!='nemo-toolathlon-resource-ledger-v1':
        raise RSIError('conditions_resource_binding_invalid')
    row=ledger['sessions'].get(sid)
    if (not isinstance(row,dict) or row.get('state')!='closed'
        or row['binding'].get('episode_id')!=report['episode_id']
        or row['binding'].get('task_id',{}).get('task_id')!=result['task_id']):
        raise RSIError('conditions_resource_binding_invalid')
    ref=row['run_ref']
    if not isinstance(ref,str) or '\\' in ref or '..' in Path(ref).parts or Path(ref).is_absolute():
        raise RSIError('conditions_resource_reference_invalid')
    run=root/ref
    if not (run.is_relative_to(base) or result['task_id']=='wc-sales-tax-summary' and run.is_relative_to(root/'runs')):
        raise RSIError('conditions_resource_reference_invalid')
    metadata=inputs.json(run/'run.json')
    if (metadata.get('workspace_owner',str(root))!=str(root) or metadata.get('task_id')!=result['task_id']
        or metadata.get('resource_profile',metadata.get('profile'))!=profile
        or metadata.get('initial_state_ref')!='initial_state.json'):
        raise RSIError('conditions_resource_binding_invalid')
    initial_raw=inputs.raw(run/'initial_state.json')
    if metadata.get('initial_state_sha256')!=hashlib.sha256(initial_raw).hexdigest():
        raise RSIError('conditions_initial_hash_mismatch')
    initial=strict_json(initial_raw)
    seed=inputs.json(base/'seed_identity.json')
    if not isinstance(seed.get('seed_sha256'),str) or not SHA.fullmatch(seed['seed_sha256']):
        raise RSIError('conditions_seed_identity_invalid')
    discovered=None;seed_seen=False;closed=False
    for line in inputs.raw(directory/'trace.jsonl',128*1024*1024).splitlines():
        if len(line)>8*1024*1024:raise RSIError('conditions_trace_line_limit')
        entry=strict_json(line);value=entry.get('value')
        if entry.get('kind')!='nemo' or not isinstance(value,dict):continue
        if value.get('route')=='/seed_session':
            if (seed_seen or value['request']!=row['binding'] or not isinstance(value.get('response'),dict)
                or value['response'].get('resources_session_id')!=sid):
                raise RSIError('conditions_trace_binding_invalid')
            seed_seen=True
        elif value.get('route')=='/tools':
            if not seed_seen or discovered is not None or value['request']!=bound:
                raise RSIError('conditions_trace_binding_invalid')
            discovered=value['response']
        elif value.get('route')=='/close_session':
            closed=discovered is not None and value.get('request')==bound and value.get('response')=={'resources_session_id':sid}
    if not closed or discovered.get('task_profile')!=report.get('task_profile') or not isinstance(report.get('task_profile'),dict):
        raise RSIError('conditions_discovery_binding_invalid')
    tools,_=tool_inventory(discovered)
    initial_digest=None
    if result['task_id']=='wc-sales-tax-summary':
        if (set(initial)!={'tables','files'} or set(initial['tables'])!=TABLES or not isinstance(initial['files'],dict)
            or any(set(table)!={'rows','sha256'} or type(table['rows']) is not int or table['rows']<0
                   or not isinstance(table['sha256'],str) or not SHA.fullmatch(table['sha256']) for table in initial['tables'].values())
            or any(not isinstance(sha,str) or not SHA.fullmatch(sha) for sha in initial['files'].values())):
            raise RSIError('conditions_initial_scope_invalid')
        initial_digest=digest(initial)
    return {'request_id':identity,'run_id':result['run_id'],
        'components':{'task_configuration':digest({'task':result['task_id'],'profile':report['task_profile'],
                          'environment':report['environment_source_sha256']}),
            'public_prompt':digest({key:prompt[key] for key in ('commit','path','git_blob','sha256')}),
            'original_tools':digest(tools),'sql_seed_record':seed['seed_sha256'],
            'recorded_initial_data':initial_digest,'clock_contract':None},
        'initial_data_scope':'tax_five_tables_and_workspace' if initial_digest else 'incomplete_task_snapshot'}


def compare_retained_conditions(root,identity):
    root=Path(root).resolve();identity=request_id(identity);inputs=Inputs(root)
    before={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in SOURCES}
    path=root/'evidence/skill_rsi/comparisons'/(identity+'.json')
    if not audit_comparison(root,path)['passed']:raise RSIError('conditions_comparison_audit_failed')
    pair=inputs.json(path)
    arms=[_one(root,job,inputs) for job in pair['arms']]
    dimensions={}
    for name in arms[0]['components']:
        hashes=[arm['components'][name] for arm in arms]
        dimensions[name]={'status':'unrecorded' if any(value is None for value in hashes)
                          else 'match' if len(set(hashes))==1 else 'different','sha256':hashes}
    inputs.unchanged()
    if any(hashlib.sha256((root/name).read_bytes()).hexdigest()!=sha for name,sha in before.items()):
        raise RSIError('conditions_source_changed')
    return {'schema_version':'skill-rsi-conditions-v1','request_id':identity,'binding_audit_passed':True,
        'task_id':pair['task_id'],'arms':arms,'dimensions':dimensions,
        'recorded_components_equal':all(row['status']=='match' for name,row in dimensions.items() if name!='clock_contract'),
        'initial_state_equality_attested':False,'underlying_model_revision_attested':False,
        'selection_eligible':False,'winner':None,'training_eligible':False,'model_http_requests':0,
        'oracle_recomputed':False,'retained_input_count':len(inputs.hashes),
        'retained_input_set_sha256':digest({path.relative_to(root).as_posix():sha for path,sha in inputs.hashes.items()}),
        'source_sha256':before,'source_unchanged':True,'scope':'retained_conditions_not_full_comparability_or_selection'}
