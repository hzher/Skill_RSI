"""Bounded, offline RSI evidence audit. Reads private inputs; emits metadata only."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from integrations.model_endpoint import EndpointError, strict_json
from integrations.model_task_executor import EXECUTION_SOURCES, append_response, stamp
from integrations.skill_rsi_contract import RSIError, descriptor_id, skill_version, validate_request
from training.skill_contract import canonical
from workenv.core import no_links

RUN_ID = re.compile('[a-f0-9]{32}')
SHA = re.compile('[a-f0-9]{64}')
AUDIT_SOURCES = ('integrations/skill_rsi_audit.py','integrations/model_task_executor.py',
                 'integrations/skill_rsi_contract.py','scripts/audit_skill_rsi.py')


def read_json(path, root):
    no_links(path,root)
    if not path.is_file() or path.stat().st_size > 1024*1024:
        raise RSIError('audit_json_unavailable')
    try:
        return strict_json(path.read_bytes())
    except EndpointError:
        raise RSIError('audit_invalid_json') from None


def request_id(value):
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except (ValueError,TypeError,AttributeError):
        pass
    raise RSIError('audit_invalid_request_id')


def audit_run(root, path):
    public = read_json(path,root)
    identity = request_id(public.get('request_id'))
    run_id, executor_id = public.get('run_id'), public.get('executor_id')
    if (path.name != identity+'.json' or not isinstance(run_id,str) or not RUN_ID.fullmatch(run_id)
        or not isinstance(executor_id,str) or not SHA.fullmatch(executor_id)):
        raise RSIError('audit_invalid_receipt_identity')
    directory = root/'.runtime/model-task-runs'/run_id
    report = read_json(directory/'result.json',root)
    snapshot = root/'.runtime/skill-rsi/executors'/executor_id
    descriptor = read_json(snapshot/'descriptor.json',root)
    if descriptor_id(descriptor) != executor_id:
        raise RSIError('audit_executor_hash_mismatch')
    names = (*EXECUTION_SOURCES,'integrations/skill_rsi_contract.py','integrations/skill_rsi_runtime.py')
    actual_names=set(descriptor.get('source_sha256',{}))
    if actual_names not in (set(names),set(names)|{'integrations/skill_rsi_locks.py'}):
        raise RSIError('audit_executor_source_set_mismatch')
    database = root/'.runtime/skill-rsi/evaluations.sqlite3'
    no_links(database,root)
    if not database.is_file() or database.stat().st_size > 64*1024*1024:
        raise RSIError('audit_store_unavailable')
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
        record = db.execute('SELECT request_json,state,request_sha256,executor_id,result_json FROM evaluations WHERE request_id=?',(identity,)).fetchone()
    if record is None or len(record[0].encode()) > 128*1024:
        raise RSIError('audit_request_unavailable')
    body = validate_request(strict_json(record[0]),[public['task_id']])
    skill = body['skill']
    checks = {
        'request_scope_bound':report['evaluation_scope']=='skill_rsi' and report['evaluation_request_id']==identity
            and body['request_id']==identity and body['task_id']==report['task_id']==public['task_id'],
        'intent_content_address_valid':record[3]==executor_id and record[2]==hashlib.sha256(canonical({'request':body,'executor_id':executor_id})).hexdigest(),
        'skill_content_address_valid':(skill_version(skill) if skill is not None else None)==public['skill_sha256']==report['skill_sha256'],
        'persistent_result_matches_public':public==_stored_json(record[4],1024*1024,'audit_store_invalid'),
        'public_diagnostic_contract_valid':public.get('schema_version')=='skill-rsi-result-v1'
            and public.get('training_eligible') is False and type(public.get('training_updates')) is int
            and public['training_updates']==0 and public.get('official_score_available') is False
            and public.get('initial_state_equality_attested') is False and public.get('sampling_deterministic') is False
            and public.get('skill_mode')==('external_skill' if skill is not None else 'no_skill_baseline')
            and public.get('trace_ref')=='model-task:'+run_id,
        'public_matches_private_result':all(public[key]==report[key] for key in
            ('run_id','status','measurement','closed','source_unchanged','model_http_requests','elapsed_seconds','model_reported_usage')),
        'completion_known':record[1]=='completed' and report['closed'] is True and report['source_unchanged'] is True,
        'model_and_budget_bound':all(report[key]==descriptor[key] for key in
            ('requested_model','protocol','max_steps','wall_seconds','max_output_tokens','reasoning_effort','endpoint_sha256','environment_source_sha256'))}
    snapshots_valid = True
    for name, digest in descriptor['source_sha256'].items():
        target = snapshot/'source'/name
        no_links(target,root)
        if not isinstance(digest,str) or not SHA.fullmatch(digest) or target.stat().st_size > 1024*1024:
            raise RSIError('audit_source_size_or_hash_invalid')
        snapshots_valid = snapshots_valid and hashlib.sha256(target.read_bytes()).hexdigest()==digest
    checks['executor_source_snapshot_valid'] = snapshots_valid
    source_names = (*EXECUTION_SOURCES,'integrations/skill_rsi_contract.py')
    if set(report.get('source_sha256',{})) != set(source_names):
        raise RSIError('audit_execution_source_set_mismatch')
    execution_sources_valid = True
    for name in source_names:
        target = directory/'source'/name
        no_links(target,root)
        if not target.is_file() or target.stat().st_size > 1024*1024:
            raise RSIError('audit_execution_source_unavailable')
        execution_sources_valid = execution_sources_valid and report['source_sha256'][name]==descriptor['source_sha256'][name] and hashlib.sha256(target.read_bytes()).hexdigest()==descriptor['source_sha256'][name]
    checks['execution_source_matches_snapshot'] = execution_sources_valid
    hashes, prefix = report.get('private_input_sha256'), directory.relative_to(root).as_posix()+'/'
    if not isinstance(hashes,dict) or not 1 <= len(hashes) <= 256 or prefix+'trace.jsonl' not in hashes:
        raise RSIError('audit_private_inputs_missing')
    valid, total = True, 0
    for name, digest in hashes.items():
        if (not isinstance(name,str) or not name.startswith(prefix) or '..' in Path(name).parts
            or not isinstance(digest,str) or not SHA.fullmatch(digest)):
            raise RSIError('audit_input_reference_invalid')
        target = root/name
        no_links(target,root)
        if not target.resolve().is_relative_to(directory.resolve()) or not target.is_file():
            raise RSIError('audit_input_reference_invalid')
        total += target.stat().st_size
        if total > 128*1024*1024:
            raise RSIError('audit_input_size_limit')
        valid = valid and hashlib.sha256(target.read_bytes()).hexdigest()==digest
    checks['all_retained_inputs_unchanged'] = valid
    trace, supplied, responses = [], [], []
    with (directory/'trace.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            if len(line) > 8*1024*1024:
                raise RSIError('audit_trace_line_limit')
            row = strict_json(line)
            trace.append(row)
            if row.get('kind')=='external_skill_input': supplied.append(row['value'])
            if row.get('kind')=='model_response': responses.append(row['value'])
    checks['close_receipt_bound'] = report.get('resources_session_id') is not None and any(
        row.get('kind')=='close' and isinstance(row.get('value'),dict)
        and row['value'].get('resources_session_id')==report['resources_session_id'] for row in trace)
    if report['model_http_requests']:
        checks['actual_skill_input_bound'] = (len(supplied)==1 and supplied[0]['skill']==skill
            and supplied[0]['skill_sha256']==public['skill_sha256']) if skill is not None else not supplied
        calls = [call for response in responses for call in append_response(report['protocol'],[],response)]
        checks['requested_tools_within_skill_permissions'] = all(call['name'] in set(skill['allowed_tools'])|{'verify_task'} for call in calls) if skill is not None else True
    else:
        checks['policy_rejected_before_model_input'] = report['failure_kind']=='skill_tool_not_available' and not supplied
    return {'request_id':identity,'run_id':run_id,'checks':checks,'passed':all(checks.values())}


def _stored_json(raw, maximum, code='audit_comparison_store_invalid'):
    if not isinstance(raw,str) or len(raw.encode('utf-8'))>maximum:
        raise RSIError(code)
    try:
        return strict_json(raw)
    except EndpointError:
        raise RSIError(code) from None


def audit_comparison(root, path):
    """Bind ordered feedback to its original intent; never certify comparability."""
    pair=read_json(path,root)
    identity=request_id(pair.get('request_id'))
    executor,source=pair.get('executor_id'),pair.get('comparison_source_sha256')
    arms=pair.get('arms')
    if (path.name!=identity+'.json' or pair.get('schema_version')!='skill-rsi-comparison-result-v1'
        or not isinstance(executor,str) or not SHA.fullmatch(executor)
        or not isinstance(source,str) or not SHA.fullmatch(source)
        or not isinstance(arms,list) or not 1<=len(arms)<=4):
        raise RSIError('audit_comparison_invalid')
    database=root/'.runtime/skill-rsi/evaluations.sqlite3'
    no_links(database,root)
    if not database.is_file() or database.stat().st_size>64*1024*1024:
        raise RSIError('audit_store_unavailable')
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        record=db.execute('SELECT * FROM comparisons WHERE request_id=?',(identity,)).fetchone()
        if record is None:
            raise RSIError('audit_comparison_record_unavailable')
        request=_stored_json(record['request_json'],128*1024)
        if (not isinstance(request,dict)
            or set(request)!={'schema_version','request_id','task_id','candidates'}
            or request['schema_version']!='skill-rsi-comparison-v1' or request['request_id']!=identity
            or not isinstance(request['task_id'],str) or not isinstance(request['candidates'],list)
            or not 2<=len(request['candidates'])<=4):
            raise RSIError('audit_comparison_store_invalid')
        candidates=[]
        for skill in request['candidates']:
            checked=validate_request({'schema_version':'skill-rsi-evaluation-v1','request_id':identity,
                'task_id':request['task_id'],'skill':skill},[request['task_id']])
            candidates.append(skill_version(checked['skill']) if checked['skill'] is not None else None)
        if len(set(candidates))!=len(candidates):
            raise RSIError('audit_comparison_store_invalid')
        results=[job['result'] for job in arms]
        ordered,stored_jobs=True,True
        for index,job in enumerate(arms):
            if index>=len(candidates):
                ordered=False
                break
            arm_id=str(uuid.uuid5(uuid.UUID(identity),'candidate:'+str(index)))
            result=job['result']
            ordered=ordered and (job['request_id']==arm_id and result['request_id']==arm_id
                and result['task_id']==request['task_id'] and result['executor_id']==executor
                and result['skill_sha256']==candidates[index])
            arm=db.execute('SELECT * FROM evaluations WHERE request_id=?',(arm_id,)).fetchone()
            if arm is None:
                stored_jobs=False
                continue
            body={'schema_version':'skill-rsi-evaluation-v1','request_id':arm_id,
                  'task_id':request['task_id'],'skill':request['candidates'][index]}
            expected_digest=hashlib.sha256(canonical({'request':body,'executor_id':executor})).hexdigest()
            expected_job={'request_id':arm_id,'request_sha256':expected_digest,'executor_id':executor,
                          'state':arm['state'],'created_at':arm['created_at'],'updated_at':arm['updated_at'],
                          'result':_stored_json(arm['result_json'],1024*1024) if arm['result_json'] else None}
            stored_jobs=stored_jobs and (arm['executor_id']==executor and arm['request_sha256']==expected_digest
                and _stored_json(arm['request_json'],128*1024)==body and job==expected_job)
        checks={
            'comparison_intent_content_address_valid':record['digest']==hashlib.sha256(canonical({
                'request':request,'executor_id':executor,'comparison_source_sha256':source})).hexdigest(),
            'comparison_store_result_matches':pair==_stored_json(record['result_json'],1024*1024),
            'candidate_order_and_child_ids_bound':ordered and pair.get('task_id')==request['task_id'],
            'persistent_arm_jobs_match':stored_jobs,
            'planned_and_observed_counts_match':type(pair.get('planned_arms')) is int
                and pair['planned_arms']==len(candidates) and type(pair.get('observed_arms')) is int
                and pair['observed_arms']==len(arms) and len(arms)<=len(candidates),
            'comparison_completion_known':record['state']=='completed' and 'error' in pair and pair['error'] is None
                and len(arms)==len(candidates) and all(job.get('state')=='completed'
                    and result.get('status') in ('passed','business_failed') and result.get('closed') is True
                    and result.get('source_unchanged') is True for job,result in zip(arms,results)),
            'same_executor':all(result['executor_id']==executor for result in results),
            'separate_attempts':len({result['run_id'] for result in results})==len(results),
            'public_arm_receipts_match':all(result==read_json(root/'evidence/skill_rsi/runs'/(
                request_id(result['request_id'])+'.json'),root) for result in results),
            'no_unattested_winner':pair.get('selection_eligible') is False and 'winner' in pair and pair['winner'] is None
                and pair.get('initial_state_equality_attested') is False,
            'no_training_or_determinism_claim':pair.get('training_eligible') is False
                and pair.get('sampling_deterministic') is False}
    return {'request_id':identity,'checks':checks,'passed':all(checks.values())}


def audit_retained(root):
    root = Path(root).resolve()
    before = {}
    for name in AUDIT_SOURCES:
        target=root/name;no_links(target,root)
        before[name]=hashlib.sha256(target.read_bytes()).hexdigest()
    paths = sorted((root/'evidence/skill_rsi/runs').glob('*.json'))
    if not paths or len(paths)>256:
        raise RSIError('audit_receipt_count_invalid')
    runs = []
    for path in paths:
        try:
            runs.append(audit_run(root,path))
        except Exception as exc:
            # Never include arbitrary paths, business text or exception strings in public output.
            try: label = request_id(path.stem)
            except RSIError: label = None
            runs.append({'receipt_id':label,'passed':False,'error':exc.code if isinstance(exc,RSIError) else 'audit_input_invalid'})
    pairs = sorted((root/'evidence/skill_rsi/comparisons').glob('*.json'))
    if len(pairs)>64:
        raise RSIError('audit_comparison_count_invalid')
    comparisons = []
    for path in pairs:
        try:
            comparisons.append(audit_comparison(root,path))
        except Exception as exc:
            comparisons.append({'passed':False,'error':exc.code if isinstance(exc,RSIError) else 'audit_comparison_invalid'})
    unchanged = all(hashlib.sha256((root/name).read_bytes()).hexdigest()==digest for name,digest in before.items())
    return {'schema_version':'skill-rsi-retained-inputs-audit-v1','observed_at':stamp(),
        'passed':unchanged and all(row['passed'] for row in [*runs,*comparisons]),
        'execution_receipts_checked':len(runs),'runs':runs,'comparisons':comparisons,
        'model_http_requests_started_by_audit':0,'training_updates':0,'oracle_recomputed':False,
        'source_scope':'historical_executor_snapshots','source_unchanged':unchanged,'source_sha256':before}
