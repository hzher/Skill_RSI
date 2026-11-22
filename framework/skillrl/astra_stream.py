"""Recorded text-only Astra streaming calls, independent of benchmark scoring."""
import json
import os
from pathlib import Path
import time
import urllib.request

from .models import ModelError
from .storage import atomic, begin, seal


class StreamText:
    def __init__(self, base_url, model, api_key_env, *, max_tokens=65536, timeout=1800):
        self.base_url, self.model, self.api_key_env = base_url, model, api_key_env
        self.max_tokens, self.timeout = max_tokens, timeout

    def call(self, messages, tools, path, **unused):
        if tools:
            raise ValueError('Text-only extraction transport cannot execute tools')
        payload = {'model': self.model, 'messages': messages, 'temperature': 0,
                   'max_tokens': self.max_tokens, 'stream': True,
                   'stream_options': {'include_usage': True}}
        path = Path(path)
        cached = begin(path, payload)
        if cached is not None:
            return cached
        key = os.environ[self.api_key_env]
        request = urllib.request.Request(self.base_url + '/chat/completions',
            json.dumps(payload).encode(), headers={'Authorization': 'Bearer ' + key,
                                                  'Content-Type': 'application/json'})
        started = time.monotonic()
        state = {'parts': [], 'model': None, 'usage': None, 'finish_reason': None, 'done': False}
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=self.timeout) as response:
                with (path / 'response.sse.raw').open('wb') as stream:
                    while line := response.readline():
                        stream.write(line)
                        stream.flush()
                        if time.monotonic() - started > self.timeout:
                            raise TimeoutError('Astra stream total deadline exceeded')
                        consume_line(line, state)
                        if state['done']:
                            break
            result = complete(state, expected_model=self.model)
            atomic(path / 'stream-receipt.json', {'raw_wire': 'response.sse.raw',
                'elapsed_seconds': time.monotonic() - started, 'response_completed': True,
                'synthesized_content': True, 'model_identity_verified': True})
            return seal(path, result)
        except Exception as exc:
            atomic(path / 'failure.json', {'kind': type(exc).__name__,
                'reason': str(exc).replace(key, '[REDACTED]')[:1200],
                'partial_wire_retained': (path / 'response.sse.raw').exists(),
                'finish_reason': state['finish_reason'], 'usage': state['usage']})
            raise ModelError('Astra streaming request incomplete; original bytes retained') from None


def consume_line(line, state):
    if not line.startswith(b'data:'):
        return
    text = line[5:].strip()
    if text == b'[DONE]':
        state['done'] = True
        return
    if not text:
        return
    chunk = json.loads(text)
    if chunk.get('error'):
        raise ModelError('Astra stream error: ' + str(chunk['error']))
    if chunk.get('model'):
        if state['model'] is not None and state['model'] != chunk['model']:
            raise ModelError('Teacher identity changed within stream')
        state['model'] = chunk['model']
    if chunk.get('usage') is not None:
        state['usage'] = chunk['usage']
    for choice in chunk.get('choices', []):
        if choice.get('index', 0) != 0:
            raise ModelError('Unexpected additional teacher choice')
        delta = choice.get('delta') or {}
        if delta.get('tool_calls'):
            raise ModelError('Teacher extraction must be text only')
        content = delta.get('content')
        if content:
            if not isinstance(content, str):
                raise ModelError('Invalid stream content')
            state['parts'].append(content)
        if choice.get('finish_reason'):
            state['finish_reason'] = choice['finish_reason']


def complete(state, expected_model):
    if not state['done'] or state['finish_reason'] != 'stop':
        raise ModelError('Missing complete text response: ' + str(state['finish_reason']))
    if state['model'] != expected_model:
        raise ModelError('Unexpected or missing teacher identity')
    return {'message': {'role': 'assistant', 'content': ''.join(state['parts'])},
            'reported_model': state['model'], 'usage': state['usage'], 'finish_reason': 'stop'}
