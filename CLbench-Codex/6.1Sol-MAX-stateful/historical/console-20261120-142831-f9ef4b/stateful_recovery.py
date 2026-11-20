"""Recover this frozen database family from verified, read-only native evidence."""
import json
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import tempfile
import time
import uuid

from console_common import atomic, digest, read, safe_id
from stateful_environment import Episode

FAMILY = 'database_exploration'
FUNCTIONS = {'abs','avg','count','datetime','length','lower','max','min','printf','row_number',
             'strftime','sum','typeof','like','round','coalesce','substr','upper','trim'}


def require(condition, message):
    if not condition:
        raise RuntimeError('Database recovery refused: '+message)


def fingerprint(path, root):
    return {'path':path.relative_to(root).as_posix(),'sha256':digest(path)}


def jsonl(path):
    with path.open(encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def boundary_inputs(root, directory, members, cases, old_launch, *, failed_next=False):
    """Accept only a clean boundary following a failed Windows headroom probe."""
    safe_id(old_launch)
    report=root/'reports/resume'/old_launch
    exit_record=read(report/'exit.json',{}) or {}
    close=read(directory/'close-receipt.json',{}) or {}
    progress=read(directory/'progress.json',{}) or {}
    error=exit_record.get('error','')
    require(exit_record.get('launch_id')==old_launch and exit_record.get('state')=='needs_attention'
            and 'GlobalMemoryStatusEx' in error and 'timed out after' in error,'prior launch is not a memory-probe timeout')
    require(close.get('closed') is True and close.get('complete') is False and not close.get('errors')
            and close.get('official_service_close')=={'database_connection_closed':True,'temporary_database_removed':True},
            'prior native service closure is unverified')
    require(directory.name==FAMILY and progress.get('state')=='needs_attention'
            and progress.get('family')==FAMILY,'family is not stopped at a known boundary')
    count=progress['visited']
    require(type(count) is int and 0<count<20 and len(members)==40,'only the pre-migration canonical prefix is supported')
    require(all((p/'result.json').is_file() for p in cases[:count])
            and not any(p.exists() for p in cases[count+int(failed_next):]),
            'noncontiguous coverage or interrupted next attempt')
    require(progress.get('last_task_id')==members[count-1][1]['id'] and exit_record.get('current_case')==cases[count-1].name,
            'stopped position differs from the sealed prefix')
    entry=read(report/'entry.json')
    require(entry['config_sha256']==digest(root/'config/account.frozen.json')
            and entry['plan_sha256']==digest(root/'config/independent-301.json'),'frozen launch inputs changed')
    snapshot=report/'adapter-source'
    previous_sources=read(snapshot/'manifest.json')['files']
    require(all(digest(snapshot/name)==sha for name,sha in previous_sources.items()),'original adapter snapshot changed')
    return count,close['closed_at'],snapshot


def validate_missing_host_attempt(case, descriptor, service, prior_case):
    prior=read(prior_case/'result.json')
    row=read(case/'result.json')
    identity=read(case/'identity.json')
    chain=read(case/'continuity.json')
    native=row['native']
    require(row['identity']==identity and row['continuity']==chain and identity['task_id']==descriptor['id']
            and identity['canonical_index']==descriptor['canonical_index'] and chain['previous_attempt_id']==prior['identity']['attempt_id']
            and chain['attempt_id']==identity['attempt_id'] and chain['thread_id']==chain['resumed_thread_id']==service['thread_id']
            and row['native_thread_ids']==[service['thread_id']] and not chain['previous_zero_action_attempts'],
            'failed attempt identity or originating native thread differs')
    require(row['configuration']==prior['configuration'] and row['complete'] is False and row['execution_complete'] is False
            and row['task_done'] is False and row['score'] is None and row['resource_closed']
            and row['mcp_calls']==row['task_actions']==native['tool_calls']==0 and not row.get('failure')
            and native['complete'] and native['host_exited'] and not native['errors'] and not native['timeout']
            and native['exit_code']==0 and not native['invalid_jsonl_lines'] and not native['missing_tool_returns']
            and not native['unexpected_tools'] and not row['journal_invalid_lines'], 'attempt is not an ended zero-action turn')
    require(not (case/'broker.jsonl').exists() or not jsonl(case/'broker.jsonl'),'real tool dispatch already exists')
    require([e['request']['method'] for e in jsonl(case/'mcp.jsonl')]==['initialize','tools/list'],
            'MCP transport has an additional request')
    cli=jsonl(case/'actor/native-cli.jsonl')
    messages=[e['item'].get('message','') for e in cli if (e.get('item') or {}).get('type')=='error']
    require(any('failed to spawn code-mode host' in m and 'codex-code-mode-host.exe: host executable was not found' in m
                for m in messages) and sum(e.get('type')=='turn.completed' for e in cli)==1,
            'missing native runtime helper is not proven')
    require(read(case/'close-receipt.json')['complete'] is True
            and read(case/'native-objective.json')['instance_outcomes']==row['objective']['instance_outcomes']==[]
            and digest(case/'initial-task.json')==digest(case/'final-task.json')==service['verified_next_query_sha256']
            and read(case/'database-state.json')['sha256']==service['reconstructed_cases'][-1]['database_sha256'],
            'failed turn changed the original query, database or native outcome')
    actor=read(case/'actor/actor-config.json')
    require(actor['auth_mode']=='chatgpt' and actor['model']==prior['configuration']['model']
            and actor['reasoning_effort']==prior['configuration']['reasoning_effort']
            and actor['resume_thread_id']==service['thread_id']
            and actor['cli_sha256']==read(prior_case/'actor/actor-config.json')['cli_sha256'],
            'failed turn was not run with the same original actor')
    check_manifest(case,row)
    delta={key:value-prior['native']['usage'][key] for key,value in native['usage'].items()}
    require(delta==row['usage_delta'] and all(v>=0 for v in delta.values()) and delta['output_tokens']>0,
            'failed native turn usage is unavailable or inconsistent')
    return row


def recovery_inputs(root,directory,members,cases,old_launch):
    """Recognize the original probe outage or the single proven tool-host failure."""
    report=root/'reports/resume'/safe_id(old_launch)
    error=(read(report/'exit.json',{}) or {}).get('error','')
    if not error.startswith('Native instance requires reconciliation:'):
        count,closed,source=boundary_inputs(root,directory,members,cases,old_launch)
        return count,closed,source,None
    stage=directory/'recoveries'/old_launch
    first=read(stage/'recovery.json')
    require(first and first['schema']=='clbench-database-service-recovery-v1' and first['launch_id']==old_launch
            and not first.get('zero_action_retry'),'failed service has no original database reconstruction')
    count,_,source=boundary_inputs(root,directory,members,cases,first['previous_launch_id'],failed_next=True)
    path=cases[count]
    task=members[count][1]
    closed=read(stage/'close-receipt.json')
    exit_record=read(report/'exit.json')
    progress=read(stage/'progress.json')
    require(exit_record.get('launch_id')==old_launch and exit_record['state']=='needs_attention'
            and exit_record['current_case']==path.name and error=='Native instance requires reconciliation: '+task['id']+' []'
            and read(report/'network-close.json')['closed'] is True and closed['closed'] is True and not closed['complete']
            and closed['official_service_close']=={'database_connection_closed':True,'temporary_database_removed':True}
            and not closed['errors'] and progress['state']=='needs_attention' and progress['visited']==count+1
            and progress['last_task_id']==task['id'],'failed service is still open or has more attempts')
    output=directory.parent.parent
    archive_dir=output/'attempt-history'/path.name
    require(archive_dir.resolve()==archive_dir and path.resolve()==path,'attempt archive or case is an alias')
    archived=list(archive_dir.iterdir()) if archive_dir.exists() else []
    require((path.is_dir() and not archived) or (not path.exists() and len(archived)==1 and archived[0].is_dir()),
            'failed case has multiple or missing copies')
    existing=path if path.exists() else archived[0]
    row=validate_missing_host_attempt(existing,task,first,cases[count-1])
    aid=safe_id(row['identity']['attempt_id'])
    target=archive_dir/aid
    require(target.resolve()==target,'archived attempt points outside the collection')
    require(existing==path or existing==target,'archived attempt does not match the original ID')
    require(row['continuity']['service_recovery_ref']==fingerprint(stage/'recovery.json',output)
            and row['continuity']['family_close_ref']==(stage/'close-receipt.json').relative_to(output).as_posix()
            and first['source_close_sha256']==digest(directory/'close-receipt.json')
            and first['source_exit_sha256']==digest(root/'reports/resume'/first['previous_launch_id']/'exit.json'),
            'failed turn and prior service receipts disagree')
    details={'task_id':task['id'],'attempt_id':aid,'attempt_directory':target.relative_to(output).as_posix(),
             'task_actions':0,'broker_calls':0,'native_outcome':None,'usage_delta':row['usage_delta'],
             'wall_seconds':row['wall_seconds'],'reason':'missing_code_mode_host'}
    for key,name in [('result_ref','result.json'),('native_events_ref','actor/native-cli.jsonl'),('actor_exit_ref','actor/actor-exit.json')]:
        details[key]={'path':(target/name).relative_to(output).as_posix(),'sha256':digest(existing/name)}
    retry={'existing':existing,'target':target,'details':details,'stage':stage,'prior_service':first}
    return count,closed['closed_at'],source,retry


def verify_session(config, original_cases, thread, directory):
    """Read only the named native thread from the pinned Windows account profile."""
    require(str(uuid.UUID(thread))==thread,'invalid native thread ID')
    from skillrl.codex_chatgpt import windows_path
    # The pinned per-user Conda executable is installed below the same Windows
    # profile used by the account actor. Refuse any ambiguous location.
    executable=Path(config['host_python'])
    homes=[p for p in executable.parents if p.parent.name.lower()=='users']
    require(len(homes)==1,'cannot identify pinned Windows profile')
    candidates=list((homes[0]/'.codex/sessions').glob('*/*/*/rollout-*-'+thread+'.jsonl'))
    require(len(candidates)==1,'native session log is missing or ambiguous')
    source=candidates[0]; initial=digest(source)
    cwd=PureWindowsPath(windows_path(directory/'workspace'))
    meta=None
    turns=[]
    current=None
    for row in jsonl(source):
        payload=row.get('payload') or {}
        if row.get('type')=='session_meta':
            require(meta is None and payload.get('id')==thread,'session metadata differs')
            meta=payload
        elif row.get('type')=='event_msg' and payload.get('type')=='task_started':
            require(current is None,'overlapping native turns')
            current={'id':payload.get('turn_id'),'user':[],'assistant':[]}
        elif row.get('type')=='response_item' and payload.get('type')=='message' and current is not None:
            role=payload.get('role')
            if role in ('user','assistant'):
                message=''.join(e.get('text','') for e in payload.get('content',[]) if isinstance(e,dict))
                current[role].append(message.replace('\r\n','\n').strip())
        elif row.get('type')=='event_msg' and payload.get('type')=='task_complete':
            require(current and current['id'] and current['id']==payload.get('turn_id'),'unpaired native turn')
            turns.append(current);current=None
    require(meta is not None and PureWindowsPath(meta.get('cwd',''))==cwd,'stored family workspace differs')
    require(current is None and len(turns)==len(original_cases) and payload.get('type')=='task_complete',
            'native session contains additional or unfinished turns')
    for turn,case in zip(turns,original_cases,strict=True):
        prompt=(case/'actor/prompt.txt').read_text(encoding='utf-8').strip()
        final=(case/'actor/final.txt').read_text(encoding='utf-8').strip()
        require(prompt in turn['user'] and turn['assistant'] and turn['assistant'][-1]==final,
                'session prompt or completed assistant output differs at '+case.name)
    require(digest(source)==initial,'native session changed while verifying')
    return {'thread_id':thread,'session_sha256':initial,'verified_turns':len(turns),'last_turn_id':turns[-1]['id']}


def check_manifest(case,row):
    from skillrl.storage import contained
    manifest=case/'artifact-manifest.json'
    require(digest(manifest)==row['artifact_manifest_sha256'],'artifact inventory changed')
    for name,spec in read(manifest).items():
        f=contained(case,name)
        require(f.stat().st_size==spec['bytes'] and digest(f)==spec['sha256'],'source hash differs at '+name)


def verify_actor_and_manifest(case,descriptor,prior,thread,config):
    r=read(case/'result.json')
    i=read(case/'identity.json');c=read(case/'continuity.json')
    require(r['complete'] and r['execution_complete'] and r['task_done'] and r['resource_closed']
            and r['identity']==i and r['continuity']==c and r['configuration']==config and not c.get('previous_zero_action_attempts'),
            'incomplete or altered source '+case.name)
    require(i['canonical_index']==descriptor['canonical_index'] and i['task_id']==descriptor['id']
            and c['previous_attempt_id']==prior and c['resumed_thread_id']==thread
            and r['native_thread_ids']==[c['thread_id']] and (not thread or c['thread_id']==thread),
            'source attempt or session chain changed')
    check_manifest(case,r)
    acct=read(case/'actor/actor-config.json')
    require(acct['auth_mode']=='chatgpt' and acct['model']==config['model'] and acct['reasoning_effort']==config['reasoning_effort'],
            'native model or effort changed')
    from console_scoring import native_verdict
    score=native_verdict(case,r,descriptor)
    require(score['native_binding']=='exact' and type(score['native_success']) is bool
            and int(score['native_success'])==r['score'],'original native score binding changed')
    return r


def apply_readonly_prefix(env, config, directory, members, cases, count, scratch):
    """Exercise the official task in its own SQLite copy; compare every recorded return."""
    from skillrl.interactive import tool_result
    from src.tasks.database_exploration.task import DEFAULT_DB_PATH
    require(env.name==FAMILY and env.task._schema_drift_enabled and env.task._pre_drift_count==20,'unrecognized native schedule')
    db=Path(env.task._temp_db_path)
    require(db.resolve()!=DEFAULT_DB_PATH.resolve() and digest(db)==digest(DEFAULT_DB_PATH),'private restore copy differs from pinned source')
    conn=env.task._conn
    require(conn.execute('PRAGMA query_only').fetchone()==(0,),'unexpected original SQLite mode')
    forbidden=[]
    def allow(code,a,b,catalog,trigger):
        permitted=(code in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_RECURSIVE)
                   or (code==sqlite3.SQLITE_READ and catalog in ('main',None))
                   or (code==sqlite3.SQLITE_FUNCTION and str(b).lower() in FUNCTIONS))
        if not permitted: forbidden.append((code,a,b))
        return sqlite3.SQLITE_OK if permitted else sqlite3.SQLITE_DENY
    conn.execute('PRAGMA query_only=ON')
    conn.set_authorizer(allow)
    previous=thread=None
    recovered=[]; rows=[]
    try:
        for (_,descriptor),case in zip(members[:count],cases[:count],strict=True):
            row=verify_actor_and_manifest(case,descriptor,previous,thread,config)
            ep=Episode(env,descriptor,scratch/case.name)
            require(not ep.auto and ep.status()==read(case/'initial-task.json'),'original task observation differs')
            events=jsonl(case/'broker.jsonl')
            require(len(events)%2==0,'incomplete recorded tool intent')
            operations=set()
            for n in range(0,len(events),2):
                sent,reply=events[n:n+2]
                oid=sent['operation_id']
                require(oid not in operations and not sent['cached_receipt'] and sent['type']=='tool_intent'
                        and reply['type']=='tool_result' and all(sent[k]==reply[k] for k in
                            ('operation_id','sequence','tool','arguments','cached_receipt'))
                        and not reply['result']['isError'],'ambiguous or failed source dispatch')
                operations.add(oid)
                if sent['tool']=='status' and not sent['arguments']:
                    value=ep.status()
                else:
                    require(sent['tool']=='act' and set(sent['arguments'])=={'action'},'unrecognized original MCP dispatch')
                    action=sent['arguments']['action']
                    require(action['action']=='ANSWER' or (action['action']=='QUERY'
                            and re.match(r'^\s*(SELECT|WITH)\b',action['content'],re.I)), 'non-read-only SQL or action')
                    value=ep.act(action)
                require(not forbidden and tool_result(value)==reply['result'],
                        'native public return differs at '+case.name+' dispatch '+str(sent['sequence']))
            require(ep.done and ep.actions==row['task_actions'] and ep.status()==read(case/'final-task.json'),
                    'terminal native task state differs')
            idx=descriptor['canonical_index']
            require([env.receipts[idx]]==row['objective']['instance_outcomes'],'native restored outcome differs')
            require(digest(db)==read(case/'database-state.json')['sha256'],'read-only database digest differs')
            previous=row['identity']['attempt_id'];thread=row['continuity']['thread_id'];rows.append(row)
            recovered.append({'task_id':descriptor['id'],'attempt_id':previous,'canonical_index':idx,
                'result_sha256':digest(case/'result.json'),'broker_sha256':digest(case/'broker.jsonl'),
                'verifier_receipt_sha256':digest(case/'native-objective.json'),'database_sha256':digest(db),
                'verified_tool_returns':len(operations),'native_actions':ep.actions})
    finally:
        conn.set_authorizer(None)
        conn.execute('PRAGMA query_only=OFF')
    require(len(env.receipts)==count and not env.terminal,'native restore advanced beyond the sealed boundary')
    env.prior_usage=rows[-1]['native']['usage']
    next_ep=Episode(env,members[count][1],scratch/'next')
    next_path=scratch/'next-query.json';atomic(next_path,next_ep.status())
    return rows,recovered,digest(next_path)


def reconstruct(env, root, members, cases, count, closed_at, old_launch, new_launch, retry=None):
    with tempfile.TemporaryDirectory(prefix='clbench-database-check-') as scratch:
        rows,history,next_query=apply_readonly_prefix(env,env.config,env.directory,members,cases,count,Path(scratch))
    chain=rows[-1]['continuity']
    native_turns=cases[:count]+([retry['existing']] if retry else [])
    session=verify_session(env.config,native_turns,chain['thread_id'],env.directory)
    closure=(retry['stage'] if retry else env.directory)/'close-receipt.json'
    receipt={'schema':'clbench-database-service-recovery-v1','family':FAMILY,
             'previous_launch_id':old_launch,'launch_id':new_launch,'previous_attempt_id':chain['attempt_id'],
             'thread_id':chain['thread_id'],'resume_canonical_index':members[count][1]['canonical_index'],
             'resume_task_id':members[count][1]['id'],'original_closed_at':closed_at,'reconstructed_at':time.time(),
             'source_close_sha256':digest(closure),'source_closure':fingerprint(closure,env.directory),
             'source_exit_sha256':digest(root/'reports/resume'/old_launch/'exit.json'),
             'reconstructed_cases':history,'native_session':session,'verified_next_query_sha256':next_query,
             'new_model_calls':0,'new_case_attempts':0,'queries_executed_on_private_read_only_copy':sum(x['native_actions']-1 for x in history),
             'native_outcomes_recomputed_for_comparison':len(history),'service_restarted':True}
    if retry:
        require(retry['prior_service']['verified_next_query_sha256']==next_query
                and retry['prior_service']['reconstructed_cases']==history,'native input or reconstructed prefix differs between services')
        receipt.update(previous_recovery_ref=fingerprint(retry['stage']/'recovery.json',env.directory),
                       zero_action_retry=retry['details'])
    return rows,receipt


def retain_failed_attempt(retry):
    src=retry['existing']
    dest=retry['target']
    if src!=dest:
        dest.parent.mkdir(parents=True,exist_ok=True)
        require(not dest.exists() and digest(src/'result.json')==retry['details']['result_ref']['sha256'],
                'failed case changed while preparing its preservation')
        src.rename(dest)
