"""Claude Code in Docker, with an optional Read catalog and a host-side model bridge."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import hashlib
import json
import secrets
import subprocess
import threading
from .storage import atomic, begin, seal, load
from .budget import Budget
from .vendor.a2o import convert_request_a2o, StreamingA2OConverter, build_tool_name_mapping

class MessagesBridge:
    def __init__(self, model, path, bind='127.0.0.1', *, max_generations=64, wall_seconds=900):
        self.model, self.path = model, Path(path)
        self.lock = threading.Lock()
        self.max_generations = max_generations
        self.budget = Budget(wall_seconds=wall_seconds)
        self.token = secrets.token_urlsafe(32)
        self.request_index = 0
        bridge = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if urlsplit(self.path).path != '/' + bridge.token + '/v1/messages':
                    self.send_error(404)
                    return
                try:
                    size = int(self.headers.get('Content-Length', 0))
                    if not 0 < size <= 16 * 1024 * 1024:
                        raise ValueError('Request size bound')
                    incoming = json.loads(self.rfile.read(size))
                    payload = convert_request_a2o(incoming)
                    for message in payload['messages']:
                        if message['role'] == 'system' and isinstance(message['content'], list):
                            if any(item.get('type') != 'text' for item in message['content']):
                                raise ValueError('Unsupported non-text system input')
                            message['content'] = '\n'.join(item['text'] for item in message['content'])
                    # No native Skill tool is exposed; Read catalog loading is autonomous.
                    with bridge.lock:
                        key = hashlib.sha256(json.dumps(incoming, sort_keys=True).encode()).hexdigest()
                        path = bridge.path / key
                        atomic(path / 'incoming.json', incoming)
                        cached = (path / 'provider/result.json').exists()
                        if not cached and bridge.request_index >= bridge.max_generations:
                            raise RuntimeError('Native generation budget exhausted')
                        if cached:
                            result = load(path / 'provider/result.json')
                        else:
                            result = bridge.budget.call(bridge.model, payload['messages'], payload.get('tools', []), path / 'provider')
                            atomic(path / 'index.json', {'index': bridge.request_index})
                            bridge.request_index += 1
                    names = build_tool_name_mapping(incoming.get('tools'))
                    converter = StreamingA2OConverter(bridge.model.model, names)
                    message = result['message']
                    delta = {'content': message.get('content')}
                    if message.get('tool_calls'):
                        delta['tool_calls'] = [{**item, 'index': i} for i, item in enumerate(message['tool_calls'])]
                    chunk = {'model': result['reported_model'], 'choices': [{'delta': delta, 'finish_reason': result['finish_reason']}],
                             'usage': result['usage']}
                    raw = b''.join([converter.start(), *converter.feed(chunk), *converter.finish()])
                    atomic(path / 'native-response.json', converter.build_final_response())
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except Exception:
                    self.send_error(400, 'Model bridge request failed; inspect retained receipts')
        self.server = ThreadingHTTPServer((bind, 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

class ClaudeCodeExecutor:
    """Native SDK runs its own Read/Write/Edit/Glob/Grep/Bash tools inside Docker.

    The configured image must already contain Claude Code. Only the task workspace
    and read-only skill snapshot are mounted. Upstream credentials remain on host.
    """
    def __init__(self, model, *, cli='/opt/claude', max_turns=64, timeout=900,
                 bridge_bind='127.0.0.1', bridge_advertise='127.0.0.1', cli_host_path=None):
        self.model, self.cli, self.max_turns = model, cli, max_turns
        self.timeout, self.bind, self.advertise = timeout, bridge_bind, bridge_advertise
        self.cli_host_path = Path(cli_host_path) if cli_host_path else None
        if self.cli_host_path:
            self.cli = "/opt/skillrl-claude"

    def settings(self):
        return {'backend': 'claude-code', 'model': self.model.settings(), 'cli': self.cli,
                'max_turns': self.max_turns, 'timeout': self.timeout,
                'bridge_bind': self.bind, 'bridge_advertise': self.advertise,
                'cli_host_path': str(self.cli_host_path) if self.cli_host_path else None}

    def execute(self, task, environment, snapshot, path):
        from .environments import DockerEnvironment
        if not isinstance(environment, DockerEnvironment):
            raise ValueError('Native Claude Code requires a Docker environment')
        path = Path(path)
        cached = begin(path, {'task': task.public(), 'snapshot': snapshot, 'settings': self.settings()})
        if cached is not None:
            return cached
        # The public skill directory was bound read-only before container start.
        catalog = [{'name': entry['name'], 'description': entry['description'],
                    'path': '/opt/skillrl-skills/' + entry['name'] + '.md'} for entry in snapshot]
        prompt = task.instruction + '\n\nOptional skill catalog. Read useful procedures or solve directly.\n' + json.dumps(catalog, ensure_ascii=False)
        bridge = MessagesBridge(self.model, path / 'native-requests', self.bind,
                                max_generations=self.max_turns, wall_seconds=self.timeout)
        port = bridge.start()
        argv = ['docker', 'exec', '-i', '-e', 'HOME=/tmp/skillrl-home',
                '-e', 'CLAUDE_CONFIG_DIR=/tmp/skillrl-home/.claude',
                '-e', 'ANTHROPIC_BASE_URL=http://' + self.advertise + ':' + str(port) + '/' + bridge.token,
                '-e', 'ANTHROPIC_API_KEY=local-bridge-token', '-e', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1',
                environment.container, self.cli, '--print', '--verbose', '--output-format', 'stream-json',
                '--permission-mode', 'bypassPermissions', '--setting-sources', '',
                '--tools', 'Read,Write,Edit,Glob,Grep,Bash', '--model', self.model.model,
                '--max-turns', str(self.max_turns)]
        try:
            result = subprocess.run(argv, input=prompt.encode(), capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            atomic(path / 'failure.json', {'kind': 'native_timeout', 'budget_seconds': self.timeout})
            raise
        finally:
            bridge.close()
        (path / 'native-cli.jsonl').write_bytes(result.stdout)
        (path / 'native-stderr.txt').write_bytes(result.stderr)
        atomic(path / 'native-exit.json', {'exit_code': result.returncode})
        native = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith(b'{')]
        terminal = [event for event in native if event.get('type') == 'result']
        if result.returncode or not terminal or terminal[-1].get('is_error'):
            raise RuntimeError('Native CLI terminal failed; preserve original execution')
        events, calls, returns = [], {}, {}
        for event in native:
            for block in event.get('message', {}).get('content', []):
                if block['type'] == 'tool_use':
                    calls[block['id']] = block
                elif block['type'] == 'tool_result':
                    returns[block['tool_use_id']] = block
        if set(calls) != set(returns):
            raise RuntimeError('Native tool returns incomplete')
        # Preserve original messages and derive the same tool call/return event contract.
        atomic(path / 'native-messages.json', native)
        for event in native:
            if event.get('type') == 'assistant':
                events.append({'type': 'model', 'message': event['message']})
            for block in event.get('message', {}).get('content', []):
                if block.get('type') == 'tool_result':
                    call = calls[block['tool_use_id']]
                    events.append({'type': 'tool', 'name': call['name'], 'arguments': call['input'],
                                   'result': block.get('content', ''), 'call_id': call['id'],
                                   'error': bool(block.get('is_error'))})
        record = {'task_id': task.id, 'events': events, 'complete': True,
                  'final': terminal[-1].get('result', ''), 'environment': environment.metadata(),
                  'native_calls': list(calls.values()), 'native_returns': list(returns.values()), 'backend': 'claude-code'}
        return seal(path, record)
