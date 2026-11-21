"""Text-only teacher using the existing native Codex account authentication.

No OAuth token handling and no API-provider fallback. The caller supplies the
extraction validator; raw prompts, CLI events and attempts remain auditable.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .storage import atomic, load


def account_environment():
    env = dict(os.environ)
    # Keep CODEX_HOME for native account auth, but disable API-key/base-url
    # overrides. --ignore-user-config bypasses configured custom providers.
    removed = []
    for name in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL',
                 'OPENAI_API_BASE', 'ASTRA_API_KEY'):
        if name in env:
            removed.append(name)
            del env[name]
    return env, removed


class AccountTeacher:
    def __init__(self, root, prompt, plan, timeout, validate):
        self.root, self.prompt, self.plan = root, prompt, plan
        self.timeout, self.validate = timeout, validate
        self.env, self.removed = account_environment()
        status = subprocess.run(['codex', 'login', 'status'], env=self.env,
                                capture_output=True, text=True, timeout=30)
        if status.returncode or 'ChatGPT' not in status.stdout + status.stderr:
            raise RuntimeError('Teacher requires existing ChatGPT account login; no API fallback')
        atomic(root / 'account-auth-receipt.json', {
            'login_status': (status.stdout + status.stderr).strip(),
            'cli_version': subprocess.check_output(['codex', '--version'], text=True).strip(),
            'provider': 'openai', 'requested_model': 'gpt-6-astra',
            'api_fallback_allowed': False, 'credentials_read_or_copied': False,
            'removed_environment_variable_names': self.removed,
            'model_identity_evidence': 'exact CLI model selection; CLI JSON does not report response model'})

    def readback(self, wanted):
        units = []
        for window in self.plan['windows']:
            if wanted & set(window['source_ids']):
                payload = load(self.root / 'inputs' / (window['id'] + '.json'))
                units += [u for u in payload['public_windows'] if u['source_id'] in wanted]
        return units

    def ask(self, name, payload, allowed):
        final = self.root / 'outputs' / (name + '.json')
        if final.exists():
            return self.validate(load(final), allowed)
        messages = [{'role': 'system', 'content': self.prompt},
                    {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
        attempts = self.root / 'native-account' / name
        attempts.mkdir(parents=True, exist_ok=True)
        first = max([int(p.name) + 1 for p in attempts.iterdir() if p.is_dir() and p.name.isdigit()] + [0])
        for ordinal in range(first, first + 3):
            stage = attempts / f'{ordinal:03d}'
            stage.mkdir()
            text = '\n\n'.join(m['role'].upper() + ':\n' + m['content'] for m in messages)
            (stage / 'prompt.txt').write_text(text)
            with tempfile.TemporaryDirectory(prefix='astra-skill-teacher-') as cwd:
                command = ['codex', 'exec', '--ignore-user-config', '--ignore-rules',
                    '--skip-git-repo-check', '--ephemeral', '--json', '-C', cwd,
                    '-m', 'gpt-6-astra', '-c', 'model_provider="openai"',
                    '-c', 'model_reasoning_effort="medium"',
                    '-c', 'features.shell_tool=false', '-c', 'features.multi_agent=false',
                    '-c', 'web_search="disabled"',
                    '-o', str(stage / 'final.txt'), '-']
                atomic(stage / 'request.json', {'argv': command,
                    'prompt_sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'allowed_source_ids': sorted(allowed), 'provider': 'openai',
                    'requested_model': 'gpt-6-astra', 'api_fallback_allowed': False})
                with (stage / 'native.jsonl').open('w') as out, (stage / 'stderr.txt').open('w') as err:
                    try:
                        result = subprocess.run(command, input=text, text=True, env=self.env,
                            stdout=out, stderr=err, timeout=self.timeout)
                        code = result.returncode
                    except subprocess.TimeoutExpired:
                        code = 124
                atomic(stage / 'exit.json', {'exit_code': code})
            if code:
                raise RuntimeError(f'Native account teacher failed ({code}); retained at {stage}; no API fallback')
            events = [json.loads(line) for line in (stage / 'native.jsonl').read_text().splitlines() if line.startswith('{')]
            if any(e.get('type') == 'item.completed' and e.get('item', {}).get('type') not in ('agent_message', 'reasoning') for e in events):
                raise RuntimeError('Text-only teacher unexpectedly used tools; inspect ' + str(stage))
            usage = next((e['usage'] for e in reversed(events) if e.get('type') == 'turn.completed'), None)
            content = (stage / 'final.txt').read_text().strip()
            if content.startswith('```'):
                content = content.split('\n', 1)[1].rsplit('```', 1)[0].strip()
            try:
                value = self.validate(json.loads(content), allowed)
                requests = value.get('readback_requests', [])
                if requests:
                    wanted = {r['source_id'] for r in requests}
                    messages += [{'role': 'assistant', 'content': content}, {'role': 'user', 'content':
                        json.dumps({'requested_public_readback': self.readback(wanted),
                                    'instruction': 'Return complete JSON; clear resolved readback_requests.'}, ensure_ascii=False)}]
                    continue
            except (ValueError, TypeError, KeyError) as exc:
                atomic(stage / 'validation-error.json', {'error': str(exc)})
                messages += [{'role': 'assistant', 'content': content}, {'role': 'user', 'content':
                    'Repair the complete JSON; preserve generality and provenance. Error: ' + str(exc)}]
                continue
            atomic(final, value)
            atomic(self.root / 'coverage' / (name + '.json'), {
                'response_complete': True, 'source_ids': sorted(allowed), 'usage': usage,
                'requested_model': 'gpt-6-astra', 'reported_model': None,
                'provider': 'openai', 'auth': 'existing ChatGPT account',
                'prompt_sha256': hashlib.sha256(text.encode()).hexdigest(), 'native_attempt': str(stage)})
            print(json.dumps({'event': 'teacher_completed', 'batch': name, 'skills': len(value['skills'])}), flush=True)
            return value
        raise ValueError('Teacher output unresolved; retained all attempts: ' + name)
