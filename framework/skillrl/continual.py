"""Persistent native sessions over an ordered stream of official benchmark tasks."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from .chat_gateway import ChatMessagesGateway
from .clbench import CLBenchTask, capture_workspace, docker
from .environments import DockerEnvironment
from .interactive import TaskBroker, TaskServer, native_arguments, run_native
from .messages import RecordingBridge
from .storage import atomic, begin, seal, writer


LOCAL_TOOLS = 'Read,Write,Edit,Glob,Grep'


def session_arguments(config, container, runtime, api, rpc, session_id, resume):
    arguments = native_arguments(container, '/workspace', runtime, api, config['model'], config.get('max_turns', 64))
    arguments[arguments.index('--tools') + 1] = LOCAL_TOOLS
    arguments[arguments.index('--allowedTools') + 1] += ',' + LOCAL_TOOLS
    environment = {
        'CLAUDE_CODE_AUTO_COMPACT_WINDOW': str(config['context_window']),
        'CLAUDE_CODE_MAX_CONTEXT_TOKENS': str(config['context_window']),
        'DISABLE_COMPACT': '', 'DISABLE_AUTO_COMPACT': '',
        'ANTHROPIC_DEFAULT_HAIKU_MODEL': config['model'],
        'ANTHROPIC_SMALL_FAST_MODEL': config['model'],
        'SKILLRL_CONTEXT_EVENTS': runtime + '/context-events',
        'SKILLRL_NOTES_DIR': '/workspace/notes',
        'API_TIMEOUT_MS': str(config.get('request_timeout', 14400) * 1000),
        'BENCHMARK_RPC_URL': rpc,
    }
    position = arguments.index(container)
    values = []
    for key, value in environment.items():
        values += ['-e', key + '=' + value]
    arguments[position:position] = values
    arguments += ['--resume' if resume else '--session-id', session_id,
                  '--debug-file', runtime + '/debug.log', '--include-hook-events']
    return arguments


def task_prompt(task, index, total):
    return (
        f'Continue the same persistent working session. This is task {index + 1}/{total}. '
        'Earlier conversation and files in /workspace/notes remain available. '
        'Use notes and other local file tools as useful for managing your working memory. '
        'Use benchmark.status and benchmark.act for the current official task environment, '
        'its files, database, commands and submissions. Local file tools operate in your separate persistent notes workspace. '
        'The current task query and action schema define its available operations and budget. '
        'When the official task is done, finish this turn with a brief summary. '
        'The next task arrives as another user turn in this same session.\n\n'
        f'Task identifier: {task["id"]}\n'
    )


def notes_snapshot(workspace, destination):
    notes = workspace / 'notes'
    manifest = {}
    for path in sorted(notes.rglob('*')):
        if path.is_file() and not path.is_symlink():
            name = str(path.relative_to(notes))
            data = path.read_bytes()
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            manifest[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    return manifest


def native_context_events(path):
    found = []
    for line in path.read_bytes().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('subtype') in ('compact_boundary', 'microcompact_boundary'):
            found.append(event)
    return found


def execute_chain(config, tasks, root):
    root = Path(root).resolve()
    intent = {'tasks': tasks, 'configuration': config, 'mode': 'persistent-native-resume-v1'}
    cached = begin(root, intent)
    if cached is not None:
        return cached
    if (root / 'CHAIN_RUNTIME.json').exists():
        raise RuntimeError('Existing persistent session requires owned continuation')
    workspace = root / 'workspace'
    (workspace / 'notes').mkdir(parents=True)
    runtime = '/workspace/.agent'
    agent_files = workspace / '.agent'
    agent_files.mkdir()
    (agent_files / 'home').mkdir()
    environment = DockerEnvironment(workspace, config['cli_image'], network=config['network'], user='0:0', security_profile='platform')
    gateway = bridge = None
    task = server = None
    owner = None
    records = []
    has_session = False
    try:
        environment.start()
        container = environment.container
        for name in ('mcp_stdio.py', 'context_hook.py'):
            shutil.copyfile(Path(__file__).with_name(name), agent_files / name)
        docker(['cp', config['cli'], container + ':' + runtime + '/claude'])
        key = subprocess.check_output(config['api_key_command']).decode().strip()
        gateway = ChatMessagesGateway(config['base_url'], key, config['model'], root / 'upstream', timeout=config['request_timeout'])
        port = gateway.start()
        bridge = RecordingBridge(f'http://127.0.0.1:{port}/v1', 'local-chat-gateway', config['model'], root / 'requests', bind=config['bridge_bind'], timeout=config['timeout'])
        api_port = bridge.start()
        api = 'http://' + config['bridge_advertise'] + ':' + str(api_port) + '/' + bridge.token
        session_id = str(uuid.uuid4())
        atomic(root / 'CHAIN_RUNTIME.json', {'session_id': session_id, 'container_id': container, 'workspace': str(workspace), 'context_window': config['context_window']})
        for index, descriptor in enumerate(tasks):
            case = root / 'cases' / f'{index + 1:03d}-{descriptor["id"]}'
            begin(case, {'task': descriptor, 'session_id': session_id, 'sequence_index': index})
            owner = ThreadPoolExecutor(max_workers=1, thread_name_prefix='benchmark')
            task = owner.submit(CLBenchTask, config['repository'], descriptor['family'], descriptor['canonical_index'], config['network']).result()
            initial = owner.submit(task.status).result()
            atomic(case / 'initial-task.json', initial)
            atomic(case / 'container-adaptations.json', task.container_adaptations)
            before_notes = notes_snapshot(workspace, case / 'notes-before')
            start_request = gateway.counter
            if task.done:
                native = None
                end_request = start_request
                complete = True
            else:
                broker = TaskBroker(task, case / 'tools', owner=owner)
                server = TaskServer(broker, config['bridge_bind'])
                rpc_port = server.start()
                rpc = 'http://' + config['bridge_advertise'] + ':' + str(rpc_port) + '/' + server.token
                mcp = {'mcpServers': {'benchmark': {'command': 'python3', 'args': [runtime + '/mcp_stdio.py'], 'env': {'BENCHMARK_RPC_URL': rpc}}}}
                atomic(agent_files / 'mcp.json', mcp)
                hook = {'type': 'command', 'command': 'python3 ' + runtime + '/context_hook.py', 'timeout': 30}
                settings = {'hooks': {
                    'Stop': [{'hooks': [{'type': 'command', 'command': 'python3 ' + runtime + '/mcp_stdio.py --stop', 'timeout': 15}]}],
                    'PreCompact': [{'hooks': [hook]}],
                    'SessionStart': [{'matcher': 'compact', 'hooks': [hook]}],
                }}
                atomic(agent_files / 'settings.json', settings)
                arguments = session_arguments(config, container, runtime, api, rpc, session_id, has_session)
                prompt = task_prompt(descriptor, index, len(tasks)) + task.query.prompt
                native = run_native(arguments, prompt, case / 'native', config['timeout'], lambda: docker(['stop', '-t', '5', container], check=False))
                end_request = gateway.counter
                receipts = [json.loads((root / 'upstream' / f'{n:04d}' / 'receipt.json').read_text()) for n in range(start_request, end_request)]
                complete = native['complete'] and task.done and not broker.failed and all(r['complete'] for r in receipts)
                if native['initializations']:
                    assert native['initializations'][0]['session_id'] == session_id
                atomic(case / 'context-events.json', native_context_events(case / 'native/native-cli.jsonl'))
                first_request = json.loads((root / 'upstream' / f'{start_request:04d}' / 'request.messages.raw').read_bytes())
                text = json.dumps(first_request.get('messages'), ensure_ascii=False)
                prior_markers = [v['task']['id'] for v in records if v['task']['id'] in text]
                atomic(case / 'continuity.json', {'session_id': session_id, 'resume': has_session, 'prior_task_markers_in_first_request': prior_markers, 'first_request_messages': len(first_request.get('messages', []))})
                has_session = True
            if task.container:
                complete = capture_workspace(task.container, task.cwd, case) and complete
                atomic(case / 'task-container-before-close.json', json.loads(docker(['inspect', task.container]).stdout))
            objective = owner.submit(task.outcome).result()
            after_notes = notes_snapshot(workspace, case / 'notes-after')
            record = {'task': descriptor, 'sequence_index': index, 'session_id': session_id, 'complete': complete,
                      'objective': objective, 'native': native, 'initialization_terminal': initial['done'],
                      'upstream_request_range': [start_request, end_request], 'notes_before': before_notes, 'notes_after': after_notes}
            seal(case, record)
            records.append(record)
            atomic(root / 'PROGRESS.json', {'time': time.time(), 'terminal': len(records), 'planned': len(tasks),
                                           'passed': sum(bool((r.get('objective') or {}).get('instance_outcomes', [{}])[0].get('success')) for r in records),
                                           'requests': gateway.counter, 'session_id': session_id})
            if server:
                server.close()
                server = None
            owner.submit(task.close).result()
            if task.container and complete:
                stopped = docker(['stop', '-t', '2', task.container], check=False)
                removed = docker(['rm', task.container], check=False)
                atomic(case / 'task-container-retired.json', {'container_id': task.container,
                       'stop_exit': stopped.returncode, 'remove_exit': removed.returncode,
                       'stop_stderr': stopped.stderr.decode(errors='replace'), 'remove_stderr': removed.stderr.decode(errors='replace')})
            task = None
            owner.shutdown()
            owner = None
            if not complete:
                raise RuntimeError('Current task requires reconciliation before the persistent chain advances')
            if config.get('gate_after') == index + 1:
                atomic(root / 'PREFIX_READY.json', {'tasks': index + 1, 'session_id': session_id, 'time': time.time()})
                deadline = time.monotonic() + 1800
                while not (root / 'PREFIX_ACCEPTED.json').exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Persistent-session prefix acceptance deadline')
                    time.sleep(2)
        return seal(root, {'complete': True, 'tasks': len(records), 'session_id': session_id, 'records': records})
    except BaseException as error:
        atomic(root / 'FAILURE.json', {'type': type(error).__name__, 'message': str(error), 'time': time.time(), 'completed_tasks': len(records)})
        raise
    finally:
        if server:
            server.close()
        if task and owner:
            owner.submit(task.close).result()
        if owner:
            owner.shutdown()
        if bridge:
            bridge.close()
        if gateway:
            gateway.close()
        environment.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--arm', choices=['grouped', 'shuffled'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    plan = json.loads(args.plan.read_text())
    tasks = {t['id']: t for t in plan['selected_tasks']}
    ordered = [tasks[key] for key in plan[args.arm + '_order']]
    output = args.output.resolve()
    with chdir(config['repository']), writer(output):
        execute_chain(config, ordered, output)


if __name__ == '__main__':
    main()
