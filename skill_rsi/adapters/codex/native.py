"""A native Codex plugin: scoped hooks feed the managed Responses gateway."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import sys
import subprocess

from ...evidence.io import digest, encode, file_lock, read_json, write_json
from ...runtime import Runtime, RuntimeEvent
from ..bridge import _hook_event
from .gateway import RequestBoundary


def build(output, *, project, state_dir, capture_id, constraints=(), resume=None, observe=True):
    root = Path(output) / 'native-marketplace'
    plugin = root / 'plugins' / 'skill-rsi'
    marketplace = 'skill-rsi-' + capture_id
    config = {'schema': 'skill-rsi.codex-native.v1', 'project': str(Path(project).resolve()),
              'state_dir': str(Path(state_dir).resolve()), 'capture_id': capture_id,
              'constraints': list(constraints), 'expected_session': resume, 'observe': observe,
              'capture_directory': str(Path(output).resolve())}
    write_json(plugin / 'bridge.json', config)
    command = shlex.join([sys.executable, '-m', 'skill_rsi.adapters.codex.native', 'hook',
                         '--config', str(plugin / 'bridge.json')])
    hooks = {name: [{'hooks': [{'type': 'command', 'command': command, 'timeout': 10}]}]
             for name in ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop')}
    write_json(plugin / 'hooks' / 'hooks.json', {'hooks': hooks})
    write_json(plugin / '.codex-plugin' / 'plugin.json', {
        'name': 'skill-rsi', 'version': '1.0.0', 'description': 'Project-scoped native event recording and gateway receipts.',
        'hooks': './hooks/hooks.json',
    })
    write_json(root / '.agents' / 'plugins' / 'marketplace.json', {'name': marketplace, 'plugins': [{
        'name': 'skill-rsi', 'source': {'source': 'local', 'path': './plugins/skill-rsi'},
    }]})
    # Invocation-only settings. User/project configuration files are never rewritten.
    return ['-c', 'features.plugins=true', '-c', 'features.codex_hooks=true',
            '-c', 'marketplaces.' + marketplace + '.source_type="local"',
            '-c', 'marketplaces.' + marketplace + '.source=' + json.dumps(str(root)),
            '-c', 'plugins.' + json.dumps('skill-rsi@' + marketplace) + '.enabled=true']


def pending_path(directory):
    return Path(directory) / 'native-pending.json'


def install(output, *, capture_id, command):
    """Explicit, scoped native installation; remove only this capture's entries."""
    root = Path(output) / 'native-marketplace'
    marketplace = 'skill-rsi-' + capture_id
    operations = []
    def invoke(args):
        result = subprocess.run([command, *args], capture_output=True, text=True, timeout=30)
        operations.append({'args': args, 'returncode': result.returncode,
                           'stdout': result.stdout, 'stderr': result.stderr})
        write_json(Path(output) / 'native-install.json', {'operations': operations})
        if result.returncode:
            raise ValueError('native Codex plugin registration failed; see native-install.json')
    invoke(['plugin', 'marketplace', 'add', str(root), '--json'])
    try:
        invoke(['plugin', 'add', 'skill-rsi@' + marketplace, '--json'])
    except BaseException:
        subprocess.run([command, 'plugin', 'marketplace', 'remove', marketplace], capture_output=True, timeout=30)
        raise
    def remove():
        receipts = []
        for args in (['plugin', 'remove', 'skill-rsi@' + marketplace],
                     ['plugin', 'marketplace', 'remove', marketplace]):
            result = subprocess.run([command, *args], capture_output=True, text=True, timeout=30)
            receipts.append({'args': args, 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        write_json(Path(output) / 'native-uninstall.json', {'operations': receipts})
    return remove


def run_hook(config, raw):
    project = Path(config['project']).resolve()
    directory = Path(config['capture_directory'])
    cwd, session = raw.get('cwd'), raw.get('session_id')
    if (not isinstance(cwd, str) or not Path(cwd).resolve().is_relative_to(project)
            or not isinstance(session, str) or not session):
        return {}
    session_file = directory / 'native-session.json'
    with file_lock(session_file.with_suffix('.lock')):
        expected = config.get('expected_session')
        if session_file.is_file():
            expected = read_json(session_file)['session_id']
        if expected and session != expected:
            return {}
        if not expected:
            write_json(session_file, {'session_id': session, 'source': 'native_hook',
                                     'capture_id': config['capture_id']})
    raw_digest = digest(encode(raw))
    event_id = str(raw.get('event_id') or raw.get('tool_use_id') or raw.get('tool_call_id') or raw_digest)
    identity = digest(encode([session, raw.get('hook_event_name'), event_id, raw_digest]))
    path = directory / 'native-events' / (identity + '.json')
    if config.get('observe'):
        write_json(path, {'schema': config['schema'], 'project': str(project), 'session_id': session,
                          'capture_id': config['capture_id'], 'event_id': identity, 'native_event': raw,
                          'visibility': {'native_hook': 'observed', 'model_input': 'unknown'}})
    registration = {'constraints': config.get('constraints', [])}
    event = _hook_event('codex', raw, project, registration)
    if event is None:
        return {}
    if event['boundary'] == 'tool_result':
        # A native hook observes this result. Its view is projected exactly once
        # at the subsequent gateway request, where replacement is verifiable.
        return {}
    runtime = Runtime(project, state_dir=config['state_dir'])
    boundary = RequestBoundary()
    decisions = runtime.dispatch(RuntimeEvent('codex', event['key'], event['boundary'], session,
        event['event_payload'], input_version=event['input_version'], run_id=config['capture_id'], thread_id=session,
        trigger_ref={'path': str(path), 'sha256': digest(path.read_bytes()) if path.is_file() else raw_digest}), boundary)
    notes = [{'operation_key': key, 'text': action['payload']['text'], 'session_id': session,
              'source_event_id': identity} for action, key in boundary.actions
             if action['kind'] == 'append_context' and isinstance(action['payload'].get('text'), str)]
    if notes:
        pending = pending_path(directory)
        with file_lock(pending.with_suffix('.lock')):
            value = read_json(pending) if pending.is_file() else {}
            for note in notes:
                value.setdefault(note['operation_key'], note)
            write_json(pending, value)
    write_json(directory / 'native-decisions' / (identity + '.json'), {
        'event_id': identity, 'decisions': [decision.to_dict() for decision in decisions],
        'delivery_status': 'queued_for_gateway' if notes else 'no_context_write',
    })
    # Context is delivered once, by the gateway after a matching session request.
    return {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['hook'])
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('native callback exceeds byte limit')
        output = run_hook(read_json(args.config), json.loads(raw))
    except Exception as error:
        print('Skill RSI native hook skipped (' + type(error).__name__ + ')', file=sys.stderr)
        output = {}
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
