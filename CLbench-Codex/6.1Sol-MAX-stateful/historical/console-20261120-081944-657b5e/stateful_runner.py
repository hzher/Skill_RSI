"""One native CL-Bench task object and one restricted personal Codex thread per family."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir
import hashlib
from itertools import groupby
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from console_common import APP, BASE, atomic, clean_error, digest, read
from console_worker import run
from stateful_environment import Episode, FamilyEnvironment
from stateful_capacity import archive_attempt, retry_window, wait_until


def freeze_sources(directory):
    directory.mkdir(parents=True,exist_ok=False)
    filenames = ('stateful_runner.py','stateful_environment.py','stateful_actor.py','stateful_capacity.py',
                 'console_worker.py','console_common.py')
    aliases={}
    for name in filenames:
        dest=directory/name
        shutil.copyfile(APP/name,dest)
        aliases[name]=digest(dest)
        if digest(APP/name)!=aliases[name]:
            raise RuntimeError('Adapter source changed during startup: '+name)
    atomic(directory/'manifest.json',{'files':aliases,'captured_at':time.time()})
    return directory/'stateful_actor.py',aliases['stateful_actor.py']


def turn_usage(env, raw, automatic, retries):
    if automatic:
        return None,'no_actor'
    prior=env.prior_usage
    env.prior_usage=raw
    if not isinstance(raw,dict) or not isinstance(prior,dict):
        return None,'missing_current_or_previous_thread_observation'
    if prior and set(prior)!=set(raw):
        return None,'inconsistent_native_usage_fields'
    if not raw or any(type(v) is not int or v<prior.get(k,0) for k,v in raw.items()):
        return None,'inconsistent_native_usage_counters'
    return ({k:v-prior.get(k,0) for k,v in raw.items()},
            'thread_delta_including_zero_action_retries' if retries else 'invocation_delta')


def host_turn(config, path, directory, episode, thread, prompt):
    from skillrl import codex_chatgpt as recorder
    from skillrl.codex_clbench import summarize_native
    from skillrl.interactive import TaskServer
    broker = recorder.JournalBroker(episode, path/'tools', owner=episode.owner, journal=path/'broker.jsonl')
    server = TaskServer(broker, '127.0.0.1')
    # server_close must drain accepted handlers before inspecting the owner's
    # journal or moving an attempt. No later write may land in a new attempt.
    server.server.daemon_threads=False
    try:
        rpc = f'http://127.0.0.1:{server.start()}/{server.token}'
        actor_script, expected_digest = episode.family.actor_source
        if digest(actor_script)!=expected_digest:
            raise RuntimeError('Frozen native host actor changed')
        spec = {'output':recorder.windows_path(path/'actor'), 'cli':config['cli'], 'model':config['model'],
                'framework_path':recorder.windows_path(BASE/'sources/Skill_RSI/framework'),
                'family_workspace':recorder.windows_path(directory/'workspace'), 'resume_thread_id':thread,
                'reasoning_effort':config['reasoning_effort'], 'timeout':config['timeout'], 'prompt':prompt,
                'mcp':{'command':'wsl.exe','args':['-d',config['wsl_distribution'],'--exec','env',
                    'BENCHMARK_RPC_URL='+rpc,'BENCHMARK_MCP_JOURNAL='+str(path/'mcp.jsonl'),config['python'],
                    str(Path(recorder.__file__).with_name('codex_mcp_stdio.py'))],
                    'startup_timeout_sec':60,'tool_timeout_sec':900,'default_tools_approval_mode':'approve'}}
        with (path/'host-launch.stdout').open('wb') as out, (path/'host-launch.stderr').open('wb') as err:
            completed = subprocess.run([config['host_python'],recorder.windows_path(actor_script)],
                          input=json.dumps(spec).encode(),stdout=out,stderr=err,timeout=config['timeout']+100)
        receipt = read(path/'actor/actor-exit.json')
        if receipt is None:
            raise RuntimeError('Missing native actor exit receipt; host exit '+str(completed.returncode))
        events, invalid = recorder.read_events(path/'actor/native-cli.jsonl')
        native = summarize_native(events,receipt['exit_code'])
        threads = list(dict.fromkeys(e['thread_id'] for e in events if e.get('type')=='thread.started'))
        unexpected = set()
        for event in events:
            item = event.get('item') or {}
            kind = item.get('type')
            if kind == 'mcp_tool_call':
                if item.get('server')!='benchmark' or item.get('tool') not in ('status','act'):
                    unexpected.add(str(item.get('server'))+'.'+str(item.get('tool')))
            elif kind and kind not in ('agent_message','reasoning','error','todo_list'):
                unexpected.add(kind)
        native.update(timeout=receipt['timeout'],invalid_jsonl_lines=invalid,unexpected_tools=sorted(unexpected))
        native['complete'] = bool(native['complete'] and not invalid and not receipt['timeout'] and not unexpected)
        native['host_exited'] = bool(not receipt['timeout'] or (read(path/'actor/actor-timeout-kill.json',{}) or {}).get('exit_code')==0)
    finally:
        server.close()
    return native, threads, broker.failed


def execute_case(config, env, owner, descriptor, path, previous, thread, progress):
    from skillrl.codex_chatgpt import artifact_manifest, match_outcome, read_events
    from skillrl.storage import begin, seal
    started = time.time()
    idx = descriptor['canonical_index']
    intent = {'family':env.name,'index':idx,'backend':'codex','auth_mode':'chatgpt','execution_mode':'stateful',
              'protocol':'native-mcp-v1','configuration':config,'previous_attempt_id':previous}
    if begin(path,intent) is not None:
        raise RuntimeError('A sealed instance cannot be grafted onto another live family')
    episode = owner.submit(Episode,env,descriptor,path).result()
    episode.owner = owner
    aid = hashlib.sha256(str(path).encode()).hexdigest()[:24]
    identity = {'family':env.name,'task_id':descriptor['id'],'canonical_index':idx,'schedule':'default',
                'instance_id':episode.instance_id,'attempt_id':aid}
    atomic(path/'identity.json',identity)
    status = owner.submit(episode.status).result()
    atomic(path/'initial-task.json',status)
    atomic(path/'task-environment.json',{'container_id':env.container,'cwd':env.cwd,'actor_location':'windows_host',
           'task_location':'official_linux_environment','tools':['benchmark.status','benchmark.act'],
           'execution_mode':'stateful','reset_scope':'family','native_task_identity':env.name})
    native = {'complete':True,'tool_calls':0,'usage':None,'errors':[]}
    threads = []
    broker_error = failure = None
    retries=[]
    try:
        if not episode.auto:
            prompt = ('Complete this one task using only the benchmark MCP tools. All task actions run in the official '
                'Linux environment and count toward its original budget. Use benchmark.status to read the current '
                'action schema and benchmark.act to apply an action. Continue until the tool result says done=true, '
                'then finish with a brief summary. The benchmark working directory is '+env.cwd+
                '. The local actor workspace is only a transport workspace.\n\n'+status['query'])
            for number in range(1,5):
                native,threads,broker_error=host_turn(config,path,env.directory,episode,thread,prompt)
                window=retry_window(native,threads,thread,episode,path,broker_error,number)
                if not window or number==4: break
                retries.append(archive_attempt(path,number,native,threads,thread,window))
                atomic(path/'actor-attempts.json',{'previous_zero_action_attempts':retries,'active_attempt':number+1})
                progress(state='waiting_capacity',retry_at=window['retry_at'],actor_attempt=number,
                         wait_reason=window['reason'])
                wait_until(window['retry_at'])
                progress(state='running',retry_at=None,wait_reason=None,actor_attempt=number+1)
            if len(threads)!=1 or (thread and threads[0]!=thread):
                raise RuntimeError('Native Codex thread continuity was not confirmed')
    except Exception as error:
        failure = {'type':type(error).__name__,'message':clean_error(error)}
        native['complete'] = False
        atomic(path/'failure.json',failure)
    atomic(path/'final-task.json',owner.submit(episode.status).result())
    owner.submit(episode.capture).result()
    valid_thread = len(threads)==1 and (not thread or threads[0]==thread)
    current = threads[0] if valid_thread else thread
    usage,usage_scope=turn_usage(env,native.get('usage') if valid_thread else None,episode.auto,retries)
    continuity = {'family':env.name, 'canonical_index':idx, 'attempt_id':aid, 'previous_attempt_id':previous,
                  'thread_id':current, 'resumed_thread_id':thread if valid_thread else None,
                  'session_started_here':bool(valid_thread and not thread), 'initialization_terminal':episode.auto,
                  'previous_zero_action_attempts':retries,'actor_attempts':0 if episode.auto else len(retries)+1,
                  'family_close_ref':'families/'+env.name+'/close-receipt.json'}
    atomic(path/'continuity.json',continuity)
    inventory = artifact_manifest(path)
    if retries:
        inventory['actor-attempts.json']={'sha256':digest(path/'actor-attempts.json'),'bytes':(path/'actor-attempts.json').stat().st_size}
    inventory['continuity.json'] = {'sha256':digest(path/'continuity.json'),'bytes':(path/'continuity.json').stat().st_size}
    atomic(path/'artifact-manifest.json',inventory)
    artifact_hash = digest(path/'artifact-manifest.json')
    # step() or native automatic advancement already produced this outcome.
    # Isolate it without recomputing the score or copying other instances.
    original = env.receipts.get(idx)
    objective = {'instance_outcomes':[original] if original is not None else [],
                 'source':'original_CLBench_instance_outcome','family':env.name}
    atomic(path/'native-objective.json',objective)
    matched, mapping = match_outcome(objective,identity)
    score = int(matched['success']) if type(matched.get('success')) is bool else None
    atomic(path/'scoring-link.json',{'identity':identity,'mapping':mapping,'query_instance_id':episode.instance_id,
           'outcome_instance_id':matched.get('instance_id'),'artifact_manifest_sha256':artifact_hash,
           'verifier_receipt_sha256':digest(path/'native-objective.json')})
    journal, invalid = read_events(path/'mcp.jsonl')
    broker_events, invalid_broker = read_events(path/'broker.jsonl')
    applied = [r for r in broker_events if r.get('type')=='tool_result' and r.get('tool')=='act'
               and not r.get('cached_receipt') and not r['result'].get('isError')]
    intents = [(r['sequence'],r.get('operation_id')) for r in broker_events if r.get('type')=='tool_intent']
    returned = [(r['sequence'],r.get('operation_id')) for r in broker_events if r.get('type')=='tool_result']
    verified = bool(episode.done and score is not None and not broker_error and not failure
                    and not invalid_broker and not invalid and len(applied)==episode.actions
                    and (episode.auto or valid_thread))
    executed = bool(native['complete'] and episode.done and not broker_error and not failure
                    and not invalid_broker and len(applied)==episode.actions and (episode.auto or valid_thread))
    safe_to_advance=bool(verified and valid_thread and episode.actions and native.get('host_exited')
                         and not native.get('unexpected_tools') and not native.get('invalid_jsonl_lines')
                         and not native.get('missing_tool_returns') and not native.get('timeout')
                         and intents==returned)
    atomic(path/'close-receipt.json',{'complete':True,'scope':'instance_transport','native_task_scope':'family',
           'family_close_ref':continuity['family_close_ref'],'closed_at':time.time()})
    record = {**intent,'identity':identity,'native':native,'native_thread_ids':threads,'continuity':continuity,
              'usage_delta':usage,'usage_delta_scope':usage_scope,'native_usage_scope':'thread_cumulative',
              'initialization_terminal':episode.auto,'model_requests':None,'initial_skills':[],'curation_calls':0,
              'training_logprobs_available':False,'policy_gradient_eligible':False,'started_at':started,
              'ended_at':time.time(),'wall_seconds':time.time()-started,'task_actions':episode.actions,
              'task_done':episode.done,'execution_complete':executed,'complete':bool(executed and not invalid and score is not None),
              'verified_native_advance_after_incomplete_actor':bool(not executed and safe_to_advance),
              'resource_closed':True,'resource_close_scope':'instance_transport','workspace_captured':True,
              'artifact_manifest_sha256':artifact_hash,'artifact_snapshot_before_evaluation':False,
              'objective':objective,'score_binding_version':1,'score_mapping':mapping,'score':score,
              'scoring_status':'scored' if score is not None else 'unavailable','native_outcome_instance_id':matched.get('instance_id'),
              'journal_invalid_lines':invalid,'mcp_calls':sum(r.get('request',{}).get('method')=='tools/call' for r in journal)}
    if failure:
        record['failure'] = failure
    return seal(path,record), current


def close_family(env, owner):
    from skillrl.clbench import docker
    from skillrl.codex_clbench import close_official_task
    errors = []
    service_close = None
    try:
        service_close=close_official_task(env,owner)
    except Exception as error:
        errors.append(clean_error(error))
    for container in env.owned:
        try:
            existing = docker(['container','ls','-aq','--no-trunc','--filter','id='+container]).stdout.decode().splitlines()
            if container in existing:
                docker(['stop','-t','5',container])
                docker(['rm',container])
            remaining = docker(['container','ls','-aq','--no-trunc','--filter','id='+container]).stdout.decode().splitlines()
            if container in remaining:
                raise RuntimeError('Owned container still present: '+container)
        except Exception as error:
            errors.append(clean_error(error))
    return {'closed':not errors,'scope':'official_family_task','owned_resources':env.owned,
            'official_service_close':service_close,'errors':errors,
            'closed_at':time.time()}


def collect_sequence(config, plan, output, boundary, worker_status=None, actor_source=None):
    from skillrl.collect import select_tasks
    from skillrl.storage import begin, seal, writer
    if config['backend']!='codex' or config['auth_mode']!='chatgpt':
        raise ValueError('Stateful collection requires the personal Codex backend')
    sys.path.insert(0, config['repository'])
    tasks = select_tasks(plan,'independent')
    families = set()
    results = []
    with writer(output), chdir(config['repository']):
        intent = {'mode':'stateful','tasks':tasks,'configuration':config}
        existing = read(output/'collection.json')
        if existing is not None and intent!=existing:
            raise RuntimeError('Stateful collection plan changed')
        atomic(output/'collection.json',intent)
        for name, group in groupby(enumerate(tasks,1), lambda item:item[1]['family']):
            if name in families:
                raise ValueError('Each stateful family must be a single contiguous sequence')
            families.add(name)
            members = list(group)
            cases = [output/'cases'/f'{n:03d}-{d["id"]}' for n,d in members]
            directory = output/'families'/name
            family_intent = {'tasks':[d for _,d in members],'configuration':config}
            cached = read(directory/'result.json')
            if cached:
                if read(directory/'intent.json')!=family_intent or not cached['complete'] or not read(directory/'close-receipt.json')['closed']:
                    raise RuntimeError('Sealed family identity or closure does not match')
                previous=None
                for (_,task),p in zip(members,cases,strict=True):
                    row=read(p/'result.json') or {}
                    ident=row.get('identity') or {}
                    chain=row.get('continuity') or {}
                    if (ident.get('task_id')!=task['id'] or ident.get('canonical_index')!=task['canonical_index']
                            or not ident.get('attempt_id') or chain.get('previous_attempt_id')!=previous):
                        raise RuntimeError('Sealed stateful instance chain is incomplete: '+p.name)
                    previous=ident['attempt_id']
                    results.append(row)
                continue
            boundary(config,cases[0],True)
            if (directory/'intent.json').exists() or any(p.exists() for p in cases):
                raise RuntimeError('Interrupted stateful family needs reconciliation before continuation: '+name)
            begin(directory,family_intent)
            (directory/'workspace').mkdir()
            env = FamilyEnvironment(config,name,directory)
            env.actor_source=actor_source
            owner = ThreadPoolExecutor(max_workers=1,thread_name_prefix='stateful-native-owner')
            previous = thread = None
            family_summary = None
            progress = {'family':name,'planned':len(members),'visited':0,'state':'running'}
            def report(**fields):
                progress.update(fields,updated_at=time.time())
                atomic(directory/'progress.json',progress)
                atomic(output.parents[2]/'reports/stateful-progress.json',progress)
                if worker_status and fields.get('state') in ('waiting_capacity','running'):
                    worker_status(state='waiting' if fields['state']=='waiting_capacity' else 'running',
                                  capacity_retry_at=progress.get('retry_at'),capacity_reason=progress.get('wait_reason'))
            report()
            try:
                owner.submit(env.start,[d['canonical_index'] for _,d in members]).result()
                for (_,task),path in zip(members,cases,strict=True):
                    boundary(config,path,False)
                    report(current_task_id=task['id'],actor_attempt=0,retry_at=None,wait_reason=None)
                    row,thread = execute_case(config,env,owner,task,path,previous,thread,report)
                    results.append(row)
                    previous = row['identity']['attempt_id']
                    report(visited=progress['visited']+1,last_task_id=task['id'],thread_id=thread)
                    if not row['execution_complete'] and not row['verified_native_advance_after_incomplete_actor']:
                        raise RuntimeError('Native instance requires reconciliation: '+task['id']+' '+str(row.get('failure') or row['native'].get('errors')))
                if len(members)==len(env.task.instances) and not env.terminal:
                    raise RuntimeError('Full native task did not terminate at the planned boundary')
                official_hash = owner.submit(env.evaluate).result()
                family_summary = {'complete':True,'visited':len(members),'native_family_objective_sha256':official_hash,
                                  'full_native_schedule':env.terminal,'thread_id':thread}
            finally:
                try:
                    receipt = close_family(env,owner)
                finally:
                    owner.shutdown()
                atomic(directory/'close-receipt.json',{**receipt,'complete':bool(receipt['closed'] and family_summary)})
                report(state='complete' if family_summary and receipt['closed'] else 'needs_attention')
            if not receipt['closed']:
                raise RuntimeError('Native family resources require reconciliation: '+name)
            seal(directory,family_summary)
    result = {'mode':'stateful','records':[{'identity':r['identity'],'complete':r['complete']} for r in results],
              'complete':all(r['complete'] for r in results),'stop_reason':None}
    atomic(output/'collection-result.json',result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--launch-id',required=True)
    args = parser.parse_args()
    def frozen_collector(config,plan,output,boundary,status):
        actor=freeze_sources(args.root/'reports/resume'/args.launch_id/'adapter-source')
        return collect_sequence(config,plan,output,boundary,status,actor)
    return run(args.root,args.launch_id,collection_runner=frozen_collector)


if __name__=='__main__':
    raise SystemExit(main())
