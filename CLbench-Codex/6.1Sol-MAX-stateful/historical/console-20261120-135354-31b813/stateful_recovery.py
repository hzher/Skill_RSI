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


def boundary_inputs(root, directory, members, cases, old_launch):
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
    require(all((p/'result.json').is_file() for p in cases[:count]) and not any(p.exists() for p in cases[count:]),
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


def verify_actor_and_manifest(case,descriptor,prior,thread,config):
    from skillrl.storage import contained
    r=read(case/'result.json')
    i=read(case/'identity.json');c=read(case/'continuity.json')
    require(r['complete'] and r['execution_complete'] and r['task_done'] and r['resource_closed']
            and r['identity']==i and r['continuity']==c and r['configuration']==config and not c.get('previous_zero_action_attempts'),
            'incomplete or altered source '+case.name)
    require(i['canonical_index']==descriptor['canonical_index'] and i['task_id']==descriptor['id']
            and c['previous_attempt_id']==prior and c['resumed_thread_id']==thread
            and r['native_thread_ids']==[c['thread_id']] and (not thread or c['thread_id']==thread),
            'source attempt or session chain changed')
    manifest=case/'artifact-manifest.json'
    require(digest(manifest)==r['artifact_manifest_sha256'],'artifact inventory changed')
    for name,spec in read(manifest).items():
        f=contained(case,name)
        require(f.stat().st_size==spec['bytes'] and digest(f)==spec['sha256'],'source hash differs at '+name)
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


def reconstruct(env, root, members, cases, count, closed_at, old_launch, new_launch):
    with tempfile.TemporaryDirectory(prefix='clbench-database-check-') as scratch:
        rows,history,next_query=apply_readonly_prefix(env,env.config,env.directory,members,cases,count,Path(scratch))
    chain=rows[-1]['continuity']
    session=verify_session(env.config,cases[:count],chain['thread_id'],env.directory)
    closure=env.directory/'close-receipt.json'
    receipt={'schema':'clbench-database-service-recovery-v1','family':FAMILY,
             'previous_launch_id':old_launch,'launch_id':new_launch,'previous_attempt_id':chain['attempt_id'],
             'thread_id':chain['thread_id'],'resume_canonical_index':members[count][1]['canonical_index'],
             'resume_task_id':members[count][1]['id'],'original_closed_at':closed_at,'reconstructed_at':time.time(),
             'source_close_sha256':digest(closure),'source_closure':fingerprint(closure,env.directory),
             'source_exit_sha256':digest(root/'reports/resume'/old_launch/'exit.json'),
             'reconstructed_cases':history,'native_session':session,'verified_next_query_sha256':next_query,
             'new_model_calls':0,'new_case_attempts':0,'queries_executed_on_private_read_only_copy':sum(x['native_actions']-1 for x in history),
             'native_outcomes_recomputed_for_comparison':len(history),'service_restarted':True}
    return rows,receipt
