"""Console launcher around the pinned shared collector; run inside WSL."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from console_common import BASE, atomic, digest, read, safe_id, clean_error


class BoundaryStop(BaseException):
    pass


def command(argv, timeout=30):
    return subprocess.check_output(argv, text=True, timeout=timeout, stderr=subprocess.PIPE).strip()


def preflight(config, root):
    provenance = read(root/'config/provenance.json')
    if provenance and (digest(root/'config/account.frozen.json') != provenance['config_sha256']
                       or digest(root/'config/independent-301.json') != provenance['plan_sha256']):
        raise RuntimeError('Frozen configuration or task plan has changed')
    pins = ((BASE/'sources/Skill_RSI', 'framework_commit'),
            (BASE/'sources/Skill_RSI-CTX', 'protocol_commit'),
            (Path(config['repository']), 'benchmark_commit'))
    checked = []
    for repo, key in pins:
        windows = command(['wslpath', '-w', str(repo)])
        argv = ['/mnt/d/Git/cmd/git.exe', '-C', windows]
        actual = command(argv+['rev-parse', 'HEAD'])
        if actual != config[key] or command(argv+['status', '--porcelain']):
            raise RuntimeError('Frozen source checkout differs: '+repo.name)
        checked.append({'source': repo.name, 'sha': actual})
    command(['docker', 'info', '--format', '{{.ServerVersion}}'], 20)
    sys.path.insert(0, str(BASE))
    import resume_campaign as resources
    resources.ROOT = root
    snapshot, problem = resource_reading(resources, config)
    return {'checked_at': time.time(), 'sources': checked, 'resources': snapshot,
            'resource_problem': problem, 'model_calls': 0,
            'scope': 'source pins, Docker daemon, host/WSL memory and disk; no task execution'}


def resource_reading(resources, config):
    try:
        snapshot=resources.resource_snapshot(config,host_timeout=120)
        required=(snapshot['host']['available_physical'],snapshot['host']['available_commit'],
                  snapshot['linux']['MemAvailable'],snapshot['free_disk'])
        if any(type(value) is not int or value<0 for value in required):
            raise ValueError('Incomplete resource sample')
    except (subprocess.TimeoutExpired,subprocess.CalledProcessError,OSError,KeyError,ValueError) as error:
        return {'checked_at':time.time(),'probe_failure':type(error).__name__},'resource_probe_unavailable'
    return snapshot,resources.resource_problem(snapshot)


def ensure_network(config, root):
    inspected = subprocess.run(['docker', 'network', 'inspect', config['network']],
                               capture_output=True, text=True, timeout=20)
    if inspected.returncode:
        command(['docker', 'network', 'create', '--internal', '--label',
                 'clbench.campaign='+str(root), config['network']], 20)
        inspected = subprocess.run(['docker', 'network', 'inspect', config['network']],
                                   capture_output=True, text=True, check=True, timeout=20)
    network = json.loads(inspected.stdout)[0]
    if not network['Internal'] or network.get('Labels', {}).get('clbench.campaign') != str(root):
        raise RuntimeError('Network ownership does not match this experiment')


def close_network(config, root):
    network = json.loads(command(['docker', 'network', 'inspect', config['network']], 20))[0]
    if network.get('Labels', {}).get('clbench.campaign') != str(root) or not network['Internal']:
        raise RuntimeError('Network ownership changed')
    if network.get('Containers'):
        return {'closed': False, 'reason': 'owned_network_still_has_containers'}
    command(['docker', 'network', 'rm', config['network']], 20)
    return {'closed': True, 'network': config['network'], 'ended_at': time.time()}


def run(root, launch_id, collection_runner=None):
    root = root.resolve(); safe_id(launch_id)
    config = read(root/'config/account.frozen.json')
    expected_mode='stateful' if collection_runner is not None else 'fresh'
    if config.get('execution_mode','fresh')!=expected_mode:
        raise ValueError('Wrong collector for the frozen execution mode; expected '+expected_mode)
    plan = read(root/'config/independent-301.json')
    log = root/'reports/resume'/launch_id
    log.mkdir(parents=True, exist_ok=False)
    state = {'launch_id': launch_id, 'pid': os.getpid(), 'started_at': time.time(), 'state': 'starting'}
    lock = threading.RLock(); stopped = threading.Event()
    def update(**values):
        with lock:
            state.update(values); state['updated_at'] = time.time()
            atomic(root/'reports/worker-state.json', state)
            atomic(root/'reports/LIVE_STATUS.json', {
                'updated_at': state['updated_at'], 'run_id': launch_id,
                'status': 'running' if state['state'] in ('starting', 'running', 'waiting') else state['state'],
                'current_case': state.get('current_case'), 'resource_gate': state.get('resource_gate')})
    def heartbeat():
        while not stopped.wait(15):
            update()
    update(); thread = threading.Thread(target=heartbeat, daemon=True); thread.start()
    network_ready = False
    try:
        update(state='waiting',resource_gate={'stop_reason':'checking_resource_headroom'})
        checked = preflight(config, root)
        atomic(log/'preflight.json', checked)
        sys.path.insert(0, str(BASE/'sources/Skill_RSI/framework'))
        from skillrl import collect
        import resume_campaign as resources
        problem=checked['resource_problem']
        while problem:
            if (root/'reports/STOP_BEFORE_NEXT_TASK').exists():
                raise BoundaryStop('operator_requested_stop_before_next_task')
            update(state='waiting',resource_gate={'stop_reason':problem,'waiting_for_memory':True})
            time.sleep(20)
            snapshot,problem=resource_reading(resources,config)
            atomic(log/'preflight-resource-latest.json',{'resources':snapshot,'resource_problem':problem})
        collect.select_tasks(plan, 'independent')
        ensure_network(config, root); network_ready = True
        atomic(log/'entry.json', {**state, 'config_sha256': digest(root/'config/account.frozen.json'),
            'plan_sha256': digest(root/'config/independent-301.json'), 'launcher_sha256': digest(__file__),
            'collector': 'stateful_runner.collect_sequence' if collection_runner else 'skillrl.collect.run_collection'})
        original = collect.execute
        def boundary(configuration, output, allow_pause=True):
            case = Path(output)
            if not (case/'result.json').exists():
                if (case/'intent.json').exists():
                    raise BoundaryStop('unresolved_attempt_requires_reconciliation: '+case.name)
                while True:
                    if allow_pause and (root/'reports/STOP_BEFORE_NEXT_TASK').exists():
                        raise BoundaryStop('operator_requested_stop_before_next_task')
                    snapshot,problem = resource_reading(resources,configuration)
                    gate = {'case': case.name, 'resources': snapshot, 'stop_reason': problem,
                            'waiting_for_memory': bool(problem)}
                    atomic(log/'resource-state.json', gate)
                    with (log/'resources.jsonl').open('a', encoding='utf-8') as handle:
                        handle.write(json.dumps(gate)+'\n')
                    update(state='waiting' if problem else 'running', current_case=case.name, resource_gate=gate)
                    if not problem: break
                    time.sleep(20)
        def guarded(configuration, family, index, output):
            boundary(configuration, output)
            case = Path(output)
            result = original(configuration, family, index, output)
            if not result.get('resource_closed'):
                raise BoundaryStop('attempt_has_unresolved_resources: '+case.name)
            return result
        if collection_runner:
            result = collection_runner(config, plan, root/'runs/full-01/collection', boundary, update)
        else:
            collect.execute = guarded
            try:
                result = collect.run_collection(config, plan, 'independent', root/'runs/full-01/collection')
            finally:
                collect.execute = original
        visited = len(result['records'])
        # A finished collection can include unscorable attempts. Keep both facts.
        end_state = 'completed' if result['complete'] else 'needs_attention'
        update(state=end_state, collection_finished=visited == len(plan['tasks']),
               collection_complete=result['complete'], stop_reason=result.get('stop_reason'))
    except BoundaryStop as error:
        reason = str(error)
        update(state='paused' if reason == 'operator_requested_stop_before_next_task' else 'needs_attention', stop_reason=reason)
    except Exception as error:
        update(state='needs_attention', error=clean_error(error))
    finally:
        if network_ready:
            try:
                closure = close_network(config, root)
            except Exception as error:
                closure = {'closed': False, 'reason': clean_error(error)}
            atomic(log/'network-close.json', closure)
            if not closure['closed']:
                update(state='needs_attention', network_close=closure)
        stopped.set(); thread.join(timeout=3)
        update(ended_at=time.time())
        atomic(log/'exit.json', state)
    return 0 if state['state'] in ('completed', 'paused') else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--launch-id')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.check:
        print(json.dumps(preflight(read(args.root/'config/account.frozen.json'), args.root)), flush=True)
        return 0
    if not args.launch_id: parser.error('--launch-id is required')
    return run(args.root, args.launch_id)


if __name__ == '__main__':
    raise SystemExit(main())
