"""Complete OpenAI-compatible requests with durable, non-replayed attempts."""
from pathlib import Path
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from .storage import atomic, begin, seal, load

class ModelError(RuntimeError):
    pass

class HTTPModel:
    def __init__(self, base_url, model, api_key_env, *, timeout=120, max_tokens=8192):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query:
            raise ValueError('Use an HTTP base URL without embedded credentials or query')
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.max_tokens = max_tokens

    def call(self, messages, tools, path, *, token_limit=None, timeout_limit=None):
        payload = {'model': self.model, 'messages': messages, 'temperature': 0,
                   'max_tokens': min(self.max_tokens, token_limit) if token_limit is not None else self.max_tokens, 'stream': False}
        if tools:
            payload.update(tools=tools, tool_choice='auto')
        path = Path(path)
        cached = begin(path, payload)
        if cached is not None:
            return cached
        key = os.environ.get(self.api_key_env, '')
        if not key:
            atomic(path / 'failure.json', {'kind': 'known_unsent', 'reason': 'API credential environment variable absent'})
            raise ModelError('Missing API credential environment variable ' + self.api_key_env)
        url = self.base_url + ('/chat/completions' if self.base_url.endswith('/v1') else '/v1/chat/completions')
        request = urllib.request.Request(url, json.dumps(payload).encode(), headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, timeout_limit) if timeout_limit is not None else self.timeout) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ModelError('Response exceeds declared byte budget')
                (path / 'response.raw').write_bytes(raw)
                body = json.loads(raw)
            choice = body['choices'][0]
            if choice.get('finish_reason') not in ('stop', 'tool_calls'):
                raise ModelError('Complete response has unsupported finish reason ' + str(choice.get('finish_reason')))
            message = choice['message']
            if message.get('role') != 'assistant':
                raise ModelError('Expected assistant response')
            calls = message.get('tool_calls') or []
            if not isinstance(calls, list):
                raise ModelError('tool_calls must be an array or null')
            ids = [item['id'] for item in calls]
            if len(ids) != len(set(ids)):
                raise ModelError('Duplicate tool call ID')
            for call in calls:
                if call.get('type') != 'function' or not isinstance(json.loads(call['function']['arguments']), dict):
                    raise ModelError('Malformed tool arguments')
            result = {'message': message, 'usage': body.get('usage'),
                      'reported_model': body.get('model'), 'finish_reason': choice['finish_reason']}
            return seal(path, result)
        except urllib.error.HTTPError as error:
            raw = error.read(16000).decode(errors='replace').replace(key, '[REDACTED]')
            error.close()
            atomic(path / 'failure.json', {'kind': 'http_rejected', 'status': error.code,
                   'request_id': error.headers.get('x-request-id'), 'retry_after': error.headers.get('Retry-After'), 'body': raw})
            raise ModelError('Provider HTTP ' + str(error.code) + '; retained failure requires explicit reconciliation') from None
        except Exception as error:
            atomic(path / 'failure.json', {'kind': 'unresolved_or_invalid', 'type': type(error).__name__,
                   'reason': str(error).replace(key, '[REDACTED]')[:1200],
                   'complete_response_saved': (path / 'response.raw').exists()})
            raise ModelError('Provider request incomplete or invalid; original attempt retained') from None

    def settings(self):
        return {'base_url': self.base_url, 'model': self.model, 'api_key_env': self.api_key_env,
                'timeout': self.timeout, 'max_tokens': self.max_tokens}

def tool(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}}

def text_content(message):
    value = message.get('content')
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('Expected text assistant content')
    return value
