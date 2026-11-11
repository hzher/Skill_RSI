"""Scripted model fixture with real local HTTP and file actions."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import copy
import json
import os
import threading
from skillrl.agents import ToolExecutor
from skillrl.benchmarks import ManifestBenchmark, JSONExactEvaluator
from skillrl.curation import Curator
from skillrl.models import HTTPModel
from skillrl.pipeline import Pipeline
from skillrl.review import Reviewer
from skillrl.vendor.library import DeltaLibrary
from skillrl.vendor.submit import render_proposal

def skill(name='sum-check', dependencies=None):
    return {'name': name, 'description': 'Check numeric aggregates from original records.',
            'applicability': 'Aggregation over public numeric business records.',
            'exclusions': 'Skip when the task defines a different aggregation.',
            'steps': ['Read the original task rules and numeric records.', 'Compute and verify the requested output.'],
            'validation': ['Output matches the requested aggregation and structure.'], 'dependencies': dependencies or []}

def proposal(operation='add', parents=None, children=None):
    if operation == 'noop':
        return {'reason': 'Existing procedures cover the observed workflow.', 'operations': []}
    return {'reason': 'Concrete observed workflow supports this reusable procedure.', 'operations': [{
        'operation': operation, 'parents': parents or [], 'skills': [skill()] if children is None else children,
        'evidence_ids': ['e00001'], 'reason': 'Original input was actually read and returned.'}]}

def seed(root, names):
    lib = DeltaLibrary(Path(root) / 'library', 'test')
    for i, name in enumerate(names):
        content = render_proposal(proposal(children=[skill(name)]))['operations'][0]['skills'][0]['content']
        lib.apply('seed-' + str(i), [{'name': 'new_skill_insert', 'arguments': {'skill_name': name, 'content': content}}])
    return lib

class Provider:
    def __init__(self, transactions=None, *, load_skill=True, invalid_effect=False, reject_role=None):
        self.transactions = transactions or [proposal('noop')]
        self.load_skill, self.invalid_effect, self.reject_role = load_skill, invalid_effect, reject_role
        self.requests = []
        provider = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                provider.requests.append(copy.deepcopy(data))
                if data['model'] == provider.reject_role:
                    self.send_response(429)
                    self.send_header('Retry-After', '120')
                    self.end_headers()
                    self.wfile.write(b'{"error":{"code":"rate_limit"}}')
                    return
                try:
                    message = provider.reply(data)
                    raw = json.dumps({'model': data['model'], 'choices': [{'message': message,
                        'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop'}],
                        'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except Exception:
                    self.send_error(500)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        os.environ['SKILLRL_TEST_KEY'] = 'fixture-only-credential'
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def model(self, role):
        return HTTPModel('http://127.0.0.1:' + str(self.server.server_port), role, 'SKILLRL_TEST_KEY')

    def call(self, name, arguments, n):
        return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-' + str(n), 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}]}

    def reply(self, data):
        messages = data['messages']
        tools = [m for m in messages if m['role'] == 'tool']
        n = len(tools)
        if data['model'] == 'executor':
            public = json.loads(messages[1]['content'])
            if not tools and public['catalog'] and self.load_skill:
                return self.call('Skill', {'name': public['catalog'][0]['name']}, n)
            reads = [m for m in tools if isinstance(json.loads(m['content']), str)]
            if not reads:
                return self.call('Read', {'path': 'input.json'}, n)
            source = json.loads(json.loads(reads[-1]['content']))
            if not any(isinstance(json.loads(m['content']), dict) and 'written' in json.loads(m['content']) for m in tools):
                return self.call('Write', {'path': 'output/result.json', 'content': json.dumps({'sum': sum(source['values'])})}, n)
            return {'role': 'assistant', 'content': 'Output written.'}
        if data['model'] == 'curator':
            if not tools:
                return self.call('Read', {'path': 'index.json'}, n)
            index = json.loads(json.loads(tools[0]['content']))
            number = int(index['task']['id'].split('-')[-1])
            candidate = self.transactions[min(number - 1, len(self.transactions) - 1)]
            calls = [c for m in messages if m['role'] == 'assistant' for c in m.get('tool_calls', [])]
            read = {json.loads(c['function']['arguments']).get('path') for c in calls if c['function']['name'] == 'Read'}
            needed = ['events/e00001.json'] + ['parents/' + parent + '.md' for delta in candidate['operations'] for parent in delta['parents']]
            for name in needed:
                if name not in read:
                    return self.call('Read', {'path': name}, n)
            if not any(c['function']['name'] == 'submit_transaction' for c in calls):
                return self.call('submit_transaction', candidate, n)
            return {'role': 'assistant', 'content': 'Actual commit or rejection receipt received.'}
        if data['model'] == 'judge':
            prompt = messages[0]['content']
            context = json.loads(prompt.split('\n', 1)[1])
            actions = [i for i, e in enumerate(context['events']) if e['type'] == 'tool' and e['name'] in ('Write', 'Edit', 'Bash')]
            if 'local_score' in prompt:
                result = {'local_score': 1, 'overall_score': 1, 'event_indices': actions, 'counterevidence_indices': [],
                    'reason': 'Subsequent output follows the recorded procedure.', 'alternative_explanations': ['Public task rules may explain the action.'],
                    'confidence': 0.7, 'uncertainties': ['Causal effect is unmeasured.']}
                if self.invalid_effect:
                    result['alternative_explanations'] = 'invalid string type'
            else:
                result = {'use_state': 'used', 'event_indices': actions, 'reason': 'Subsequent output action observed.',
                    'confidence': 0.7, 'uncertainties': []}
            return {'role': 'assistant', 'content': json.dumps(result)}
        raise ValueError('Unknown fixture role')

def bundle(root, count=1, expected=6):
    root = Path(root)
    private = root / 'private-answers'
    private.mkdir()
    rows = []
    for n in range(1, count + 1):
        public = root / ('public-' + str(n))
        public.mkdir()
        (public / 'input.json').write_text(json.dumps({'values': [n, 2, 3]}))
        (private / ('task-' + str(n) + '.json')).write_text(json.dumps({'sum': expected + n - 1}))
        rows.append({'id': 'task-' + str(n), 'instruction': 'Read input.json and write the sum to output/result.json.', 'public_dir': public.name})
    manifest = root / 'tasks.json'
    manifest.write_text(json.dumps(rows))
    return ManifestBenchmark(manifest, evaluator=JSONExactEvaluator(private))

def pipeline(root, benchmark, provider):
    return Pipeline(Path(root) / 'run', benchmark, ToolExecutor(provider.model('executor')),
        Curator(provider.model('curator')), Reviewer(provider.model('judge')), sequence='test')
