"""Account-backed text judge, with native authentication and recorded Responses."""
import json
import subprocess
import tempfile
from pathlib import Path

from .account_teacher import account_environment, isolated_account_home
from .codex import ResponsesRecorder
from .models import ModelError
from .storage import atomic, begin, seal, load


def account_call(model, messages, tools, path, *, timeout_limit=None):
    if tools:
        raise ValueError('Account text adapter does not support tools')
    path = Path(path).resolve()
    cached = begin(path, {'messages': messages, 'settings': model.settings(), 'tools': []})
    if cached is not None:
        return cached
    env, removed = account_environment()
    timeout = min(model.timeout, timeout_limit) if timeout_limit is not None else model.timeout
    prompt = '\n\n'.join(m['role'].upper() + ':\n' + m['content'] for m in messages)
    with isolated_account_home(env), tempfile.TemporaryDirectory(prefix='skillrl-account-judge-') as cwd:
        with ResponsesRecorder(model.base_url, 'account-no-api-key', path / 'wire', timeout, account=True, transport=model.transport) as url:
            provider = '{name="Account recorded",base_url=' + json.dumps(url) + ',requires_openai_auth=true,supports_websockets=false,wire_api="responses",request_max_retries=0,stream_max_retries=0}'
            argv = ['codex', 'exec', '--ignore-user-config', '--ignore-rules', '--skip-git-repo-check',
                    '--ephemeral', '--json', '-C', cwd, '-m', model.model,
                    '-c', 'model_provider="account_recorded"', '-c', 'model_providers.account_recorded=' + provider,
                    '-c', 'model_reasoning_effort="medium"', '-c', 'features.shell_tool=false',
                    '-c', 'features.multi_agent=false', '-c', 'web_search="disabled"',
                    '-o', str(path / 'final.txt'), '-']
            atomic(path / 'command.json', {'argv': argv, 'auth_mode': 'chatgpt_account',
                'credentials_copied': False, 'removed_environment_names': removed,
                'token_limit_enforced': False})
            with (path / 'native.jsonl').open('w') as out, (path / 'stderr.txt').open('w') as err:
                try:
                    result = subprocess.run(argv, input=prompt, text=True, env=env, stdout=out, stderr=err, timeout=timeout)
                except subprocess.TimeoutExpired:
                    atomic(path / 'failure.json', {'kind': 'timeout'})
                    raise ModelError('Account judge timed out') from None
    atomic(path / 'exit.json', {'exit_code': result.returncode})
    events = [json.loads(line) for line in (path / 'native.jsonl').read_text().splitlines() if line.startswith('{')]
    wires = [load(p) for p in sorted((path / 'wire').glob('*/result.json'))]
    if result.returncode or not wires or not all(w.get('response_completed') and w.get('reported_model') == model.model for w in wires):
        raise ModelError('Account judge incomplete or model identity mismatch; inspect receipts')
    if any(e.get('type') == 'item.completed' and e['item']['type'] not in ('agent_message', 'reasoning') for e in events):
        raise ModelError('Text judge unexpectedly invoked a tool')
    usage = next((e['usage'] for e in reversed(events) if e.get('type') == 'turn.completed'), None)
    if usage is None:
        raise ModelError('Account judge turn incomplete')
    return seal(path, {'message': {'role': 'assistant', 'content': (path / 'final.txt').read_text()},
                       'reported_model': model.model, 'usage': usage, 'finish_reason': 'stop'})
