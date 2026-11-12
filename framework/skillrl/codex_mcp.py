"""Small stdio MCP transport for benchmark tools; no model credentials required."""
import json
from pathlib import Path
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
import re


def serve(config):
    tools = {item['name']: item for item in config['tools']}
    skills = config.get('skills', {})
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        method = request['method']
        try:
            if method == 'initialize':
                result = {'protocolVersion': request['params']['protocolVersion'],
                          'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'skillrl-benchmark', 'version': '1'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                catalog = [{k: item[k] for k in ('name', 'description', 'inputSchema')}
                           for item in tools.values()]
                if skills:
                    catalog.append({'name': 'Skill', 'description': 'Read one optional complete skill by name.',
                                    'inputSchema': {'type': 'object', 'properties': {
                                        'name': {'type': 'string', 'enum': list(skills)}},
                                        'required': ['name'], 'additionalProperties': False}})
                if config.get('memory'):
                    catalog.append({'name': 'memory', 'description': 'Read or maintain durable user/team memory across sessions. Save only durable facts/preferences; replace obsolete rules and keep temporary instructions scoped.',
                        'inputSchema': {'type': 'object', 'properties': {
                            'action': {'type': 'string', 'enum': ['read', 'add', 'replace', 'remove']},
                            'text': {'type': 'string'}, 'target': {'type': 'string'}},
                            'required': ['action'], 'additionalProperties': False}})
                if config.get('sessions'):
                    catalog.append({'name': 'session_search', 'description': 'Search authorized prior sessions. Returns original matching session messages, not inferred facts. Use query terms or an exact session_id.',
                        'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'},
                            'session_id': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 8}},
                            'required': ['query'], 'additionalProperties': False}})
                result = {'tools': catalog}
            elif method == 'tools/call':
                name, args = request['params']['name'], request['params'].get('arguments', {})
                started = time.monotonic()
                status = 200
                if name == 'session_search' and config.get('sessions'):
                    history_path = Path(config['sessions'])
                    history = json.loads(history_path.read_text()) if history_path.exists() else []
                    tokens = set(re.findall(r'[\w-]+', args.get('query', '').lower()))
                    ranked = []
                    for session in history:
                        if args.get('session_id') and session.get('session_id', session.get('id')) != args['session_id']:
                            continue
                        body = json.dumps(session, ensure_ascii=False).lower()
                        score = sum(token in body for token in tokens)
                        if score or args.get('session_id') or not tokens:
                            ranked.append((score, session))
                    ranked.sort(key=lambda pair: pair[0], reverse=True)
                    value = {'sessions': [s for _, s in ranked[:max(1, min(8, args.get('limit', 5)))]],
                             'matched': len(ranked), 'history_sessions': len(history)}
                elif name == 'memory' and config.get('memory'):
                    memory_path = Path(config['memory'])
                    entries = json.loads(memory_path.read_text()) if memory_path.exists() else []
                    action = args['action']
                    if action in ('replace', 'remove'):
                        matches = [i for i, e in enumerate(entries) if e == args.get('target')]
                        if len(matches) != 1:
                            raise ValueError('target must match exactly one current memory entry')
                        index = matches[0]
                        if action == 'remove':
                            entries.pop(index)
                        else:
                            entries[index] = args['text']
                    elif action == 'add':
                        if not args.get('text', '').strip():
                            raise ValueError('Nonempty durable text required')
                        entries.append(args['text'])
                    elif action != 'read':
                        raise ValueError('Unknown memory action')
                    if len(json.dumps(entries)) > 16000:
                        raise ValueError('Memory envelope exceeded; consolidate existing entries')
                    if action != 'read':
                        temporary = memory_path.with_suffix('.tmp')
                        temporary.write_text(json.dumps(entries, ensure_ascii=False))
                        temporary.replace(memory_path)
                    value = {'action': action, 'entries': entries, 'persisted': True}
                elif name == 'Skill':
                    value = skills[args['name']]
                else:
                    item = tools[name]
                    req = urllib.request.Request(item['url'], data=json.dumps(args).encode(),
                        method=item.get('method', 'POST'), headers={'Content-Type': 'application/json'})
                    try:
                        with urllib.request.urlopen(req, timeout=60) as response:
                            status, value = response.status, json.load(response)
                    except urllib.error.HTTPError as exc:
                        status, value = exc.code, exc.read().decode(errors='replace')
                event = {'type': 'tool', 'name': name, 'arguments': args, 'result': value,
                         'timestamp': datetime.now(timezone.utc).isoformat(),
                         'call_id': str(request['id']), 'error': status >= 400,
                         'response_status': status, 'endpoint_url': tools.get(name, {}).get('url', ''),
                         'latency_ms': (time.monotonic() - started) * 1000}
                with Path(config['events']).open('a') as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + '\n')
                result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}],
                          'isError': status >= 400}
            else:
                raise ValueError('Unsupported MCP method ' + method)
            response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
        except Exception as exc:
            if method == 'tools/call':
                params = request.get('params', {})
                failed = {'type': 'tool', 'name': params.get('name'),
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'arguments': params.get('arguments', {}), 'result': {'error': str(exc)},
                    'call_id': str(request['id']), 'error': True, 'response_status': 500,
                    'endpoint_url': '', 'latency_ms': 0}
                with Path(config['events']).open('a') as stream:
                    stream.write(json.dumps(failed, ensure_ascii=False) + '\n')
            response = {'jsonrpc': '2.0', 'id': request['id'],
                        'error': {'code': -32603, 'message': type(exc).__name__ + ': ' + str(exc)}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    serve(json.loads(Path(sys.argv[1]).read_text()))
