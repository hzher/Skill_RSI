"""Model tool loops with a single observable event stream and optional Skill loading."""
from pathlib import Path
import json
from .models import tool, text_content
from .budget import Budget
from .storage import atomic, begin, seal, canonical, contained, load

EXECUTOR_SYSTEM = '''Complete the public task in its workspace. Follow its permissions and delivery requirements.
Choose tools and optional skills based on their public descriptions and contents. You may inspect task inputs first,
solve directly, inspect a skill, or skip unrelated skills. Skill contents are reference procedures; current task rules take priority.
Only report actions you actually performed. The complete skill catalog is public metadata, not a required checklist.'''

class ToolExecutor:
    def __init__(self, model, *, max_turns=64, wall_seconds=900):
        self.model, self.max_turns, self.wall_seconds = model, max_turns, wall_seconds

    def settings(self):
        return {'backend': 'openai-tools', 'model': self.model.settings(), 'max_turns': self.max_turns, 'wall_seconds': self.wall_seconds}

    def execute(self, task, environment, snapshot, path):
        path = Path(path)
        cached = begin(path, {'task': task.public(), 'snapshot': snapshot, 'settings': self.settings()})
        if cached is not None:
            return cached
        catalog = [{k: row[k] for k in ('name', 'skill_id', 'description', 'raw_sha256')} for row in snapshot]
        messages = [{'role': 'system', 'content': EXECUTOR_SYSTEM},
                    {'role': 'user', 'content': json.dumps({'task': task.public(), 'catalog': catalog}, ensure_ascii=False)}]
        tools = [tool('Read', 'Read a public workspace file by relative path.', {'path': {'type': 'string'}}, ['path']),
                 tool('Write', 'Write an output file in the workspace.',
                      {'path': {'type': 'string'}, 'content': {'type': 'string'}}, ['path', 'content']),
                 tool('Skill', 'Optionally load one complete skill by its exact public name.',
                      {'name': {'type': 'string', 'enum': [row['name'] for row in snapshot]}}, ['name'])]
        if not snapshot:
            tools = [item for item in tools if item['function']['name'] != 'Skill']
        if hasattr(environment, 'bash'):
            tools.append(tool('Bash', 'Run a shell command in the isolated task container.',
                              {'command': {'type': 'string'}}, ['command']))
        lookup = {row['name']: row for row in snapshot}
        events = []
        seen_calls = set()
        budget = Budget(wall_seconds=self.wall_seconds)
        for turn in range(self.max_turns):
            response = budget.call(self.model, messages, tools, path / 'requests' / f'{turn:04d}')
            message = response['message']
            events.append({'type': 'model', 'request': f'requests/{turn:04d}', 'message': message})
            messages.append(message)
            calls = message.get('tool_calls', [])
            if not calls:
                record = {'task_id': task.id, 'events': events, 'final': text_content(message),
                          'complete': True, 'budget': budget.result(), 'environment': environment.metadata() if hasattr(environment, 'metadata') else {}}
                atomic(path / 'events.json', events)
                return seal(path, record)
            for call in calls:
                if call['id'] in seen_calls:
                    raise ValueError('Repeated tool identity across model turns')
                seen_calls.add(call['id'])
                name = call['function']['name']
                args = json.loads(call['function']['arguments'])
                error = False
                try:
                    if name == 'Read' and set(args) == {'path'}:
                        result = environment.read(args['path'])
                    elif name == 'Write' and set(args) == {'path', 'content'}:
                        result = environment.write(args['path'], args['content'])
                    elif name == 'Skill' and set(args) == {'name'}:
                        entry = lookup[args['name']]
                        result = {key: entry[key] for key in ('name', 'skill_id', 'raw_sha256', 'content')}
                    elif name == 'Bash' and set(args) == {'command'} and hasattr(environment, 'bash'):
                        result = environment.bash(args['command'])
                    else:
                        raise ValueError('Unknown tool or invalid arguments')
                except (ValueError, OSError, KeyError) as failure:
                    error, result = True, {'error': str(failure)}
                content = json.dumps(result, ensure_ascii=False)
                events.append({'type': 'tool', 'name': name, 'arguments': args, 'result': result,
                               'call_id': call['id'], 'error': error, 'forwarded_turn': turn + 1})
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': content})
                atomic(path / 'events.json', events)
        raise RuntimeError('Executor generation budget exhausted; retained trace requires disposition')


def exposures(execution, snapshot, actor_path, action_tools=('Write', 'Edit', 'Bash')):
    """Full successful Skill return must occur in an actual later complete model input."""
    if execution.get('backend') == 'codex':
        return codex_exposures(execution, snapshot, actor_path, action_tools)
    if execution.get('backend') == 'claude-code':
        return native_exposures(execution, snapshot, actor_path, action_tools)
    result = []
    for entry in snapshot:
        proof = []
        for index, event in enumerate(execution['events']):
            if event['type'] != 'tool' or event['name'] != 'Skill' or event['error']:
                continue
            returned = event['result']
            if returned.get('skill_id') != entry['skill_id'] or returned.get('content') != entry['content']:
                continue
            request_path = Path(actor_path) / 'requests' / f"{event['forwarded_turn']:04d}"
            if not (request_path / 'result.json').exists():
                continue
            request = load(request_path / 'intent.json')
            if any(message.get('role') == 'tool' and message.get('tool_call_id') == event['call_id']
                   and json.loads(message['content']) == returned for message in request['messages']):
                proof.append({'event_index': index, 'call_id': event['call_id'],
                              'next_request': str(request_path.relative_to(actor_path)),
                              'subsequent_actions': [i for i, event_after in enumerate(execution['events'])
                                  if i > index and event_after['type'] == 'tool' and not event_after['error']
                                  and event_after['name'] in action_tools]})
        result.append({'name': entry['name'], 'skill_id': entry['skill_id'], 'raw_sha256': entry['raw_sha256'],
                       'body_proven': bool(proof), 'proof': proof})
    return result


def codex_exposures(execution, snapshot, actor_path, action_tools):
    """Require the exact Skill body in an actual completed Responses input."""
    requests = []
    for path in sorted((Path(actor_path) / 'wire').glob('*/request.json')):
        if not (path.parent / 'result.json').exists():
            continue
        raw = (path.parent / 'response.raw').read_text(errors='replace')
        if 'response.completed' not in raw:
            continue
        requests.append((str(path.relative_to(actor_path)), load(path)))
    rows = []
    for version in snapshot:
        proof = []
        for index, event in enumerate(execution['events']):
            if event.get('name') != 'Skill' or event.get('error'):
                continue
            if event['result'].get('raw_sha256') != version['raw_sha256'] or event['result'].get('content') != version['content']:
                continue
            for name, request in requests:
                for item in request.get('input', []):
                    output = item.get('output', '')
                    def contains_body(value):
                        if isinstance(value, str):
                            if version['content'] in value:
                                return True
                            try:
                                decoded = json.loads(value)
                            except ValueError:
                                return False
                            return decoded != value and contains_body(decoded)
                        if isinstance(value, dict):
                            return any(contains_body(v) for v in value.values())
                        if isinstance(value, list):
                            return any(contains_body(v) for v in value)
                        return False
                    if item.get('type') == 'function_call_output' and contains_body(output):
                        proof.append({'event_index': index, 'next_request': name,
                                      'subsequent_actions': [i for i, e in enumerate(execution['events'])
                                          if i > index and e['type'] == 'tool' and not e['error'] and e['name'] in action_tools]})
        rows.append({'name': version['name'], 'skill_id': version['skill_id'], 'raw_sha256': version['raw_sha256'],
                     'body_proven': bool(proof), 'proof': proof})
    return rows


def native_exposures(execution, snapshot, actor_path, action_tools=('Write', 'Edit', 'Bash')):
    """Bind exact SDK Read text to its tool ID in a completed, saved model request."""
    import re
    def text(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return '\n'.join(item['text'] for item in value if item.get('type') == 'text')
        return ''
    def plain(value):
        return re.sub(r'(?m)^\s*\d+(?:\t|→)', '', text(value))
    requests = []
    for path in (Path(actor_path) / 'native-requests').glob('*/provider/result.json'):
        requests.append((path.parent.parent, load(path.parent / 'intent.json')))
    rows = []
    for version in snapshot:
        proof = []
        for index, event in enumerate(execution['events']):
            if event['type'] != 'tool' or event['name'] != 'Read' or event['error']:
                continue
            expected = '/opt/skillrl-skills/' + version['name'] + '.md'
            if event['arguments'].get('file_path') != expected or version['content'].strip() not in plain(event['result']):
                continue
            for path, request in requests:
                matches = [message for message in request['messages'] if message.get('role') == 'tool'
                           and message.get('tool_call_id') == event['call_id']
                           and version['content'].strip() in plain(message.get('content'))]
                if matches:
                    proof.append({'event_index': index, 'call_id': event['call_id'],
                                  'next_request': str(path.relative_to(actor_path)),
                                  'subsequent_actions': [i for i, later in enumerate(execution['events'])
                                      if i > index and later['type'] == 'tool' and not later['error']
                                      and later['name'] in action_tools]})
                    break
        rows.append({'name': version['name'], 'skill_id': version['skill_id'], 'raw_sha256': version['raw_sha256'],
                     'body_proven': bool(proof), 'proof': proof})
    return rows
