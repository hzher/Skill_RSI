"""Small stdio MCP transport for benchmark tools; no model credentials required."""
import json
from pathlib import Path
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
import re


def base_catalog(config, extra=()):
    """The same authoritative public catalog used by serving and capability checks."""
    skills = config.get('skills', {})
    catalog = [{k: item[k] for k in ('name', 'description', 'inputSchema')} for item in config['tools']]
    catalog.extend(extra)
    authoring = config.get('skill_write', {}).get('contract') == 'public-skill-authoring-v1'
    if skills or authoring:
        catalog.append({'name': 'Skill', 'description': 'Read one optional complete skill by name.',
                        'inputSchema': {'type': 'object', 'properties': {
                            'name': {'type': 'string'} if authoring else {'type': 'string', 'enum': list(skills)}},
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
    if config.get('skill_write'):
        authoring = config['skill_write'].get('contract') == 'public-skill-authoring-v1'
        catalog.append({'name': 'skill_manage',
            'description': ('Create or edit a durable skill when requested by the public task. Supply complete Markdown with name and description frontmatter (maximum 3500 characters). For edits, read the full current skill with Skill first and supply expected_sha256. A successful response confirms actual persistence.' if authoring else 'Edit an existing durable skill. Read its full current version with Skill first; supply its raw_sha256 and full replacement Markdown. A successful response confirms persistence.'),
            'inputSchema': {'type': 'object', 'properties': {
                'action': {'type': 'string', 'enum': ['create', 'edit'] if authoring else ['edit']},
                'name': {'type': 'string'} if authoring else {'type': 'string', 'enum': list(skills)},
                'content': {'type': 'string'}, 'expected_sha256': {'type': 'string'},
                'reason': {'type': 'string'}},
                'required': ['action', 'name', 'content', 'reason'] if authoring else ['action', 'name', 'content', 'expected_sha256', 'reason'],
                'additionalProperties': False}})
    return catalog


def search_sessions(config, args):
    """The original authorized history search, shared by explicit read expansions."""
    if not config.get('sessions'): raise ValueError('Authorized history is disabled')
    history_path = Path(config['sessions'])
    history = json.loads(history_path.read_text()) if history_path.exists() else []
    tokens = set(re.findall(r'[\w-]+', args.get('query', '').lower()))
    ranked = []
    for session in history:
        if args.get('session_id') and session.get('session_id', session.get('id')) != args['session_id']:
            continue
        body = json.dumps(session, ensure_ascii=False).lower()
        score = sum(token in body for token in tokens)
        if score or args.get('session_id') or not tokens: ranked.append((score, session))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return {'sessions': [s for _, s in ranked[:max(1, min(8, args.get('limit', 5)))]],
            'matched': len(ranked), 'history_sessions': len(history)}


def serve(config):
    from .plugins import PluginHarness
    harness = PluginHarness(config)
    plugins = harness.plugins
    _,plugin_tools = harness.tools()
    tools = {item['name']: item for item in config['tools']}
    if set(plugin_tools) & set(tools):
        raise ValueError('Plugin tool name collides with benchmark tool')
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
                          'serverInfo': {'name': 'skill-rsi-codex-native', 'version': '2'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                extra,plugin_tools = harness.tools()
                catalog = base_catalog(config, extra)
                result = {'tools': catalog}
            elif method == 'tools/call':
                name, args = request['params']['name'], request['params'].get('arguments', {})
                if name in plugin_tools:
                    value = plugin_tools[name].handle(name, args, request['id'])
                    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {
                        'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}],
                        'isError': False}}, ensure_ascii=False), flush=True)
                    continue
                started = time.monotonic()
                status = 200
                harness.before(name, args, request['id'])
                if name == 'session_search' and config.get('sessions'):
                    value = search_sessions(config, args)
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
                    from .skill_access import current_skill
                    value = current_skill(config, args['name'])
                elif name == 'skill_manage':
                    if config.get('skill_write', {}).get('contract') == 'public-skill-authoring-v1':
                        from .skill_authoring import save_skill
                        value = save_skill(config, args, request['id'])
                    else:
                        raise ValueError('No supported explicit skill authoring capability')
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
                # Observers must see the same public view as the log and model.
                # bounded_mcp's codec may turn nested history into references.
                event = json.loads(json.dumps(event, ensure_ascii=False))
                value = event['result']
                with Path(config['events']).open('a') as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + '\n')
                result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}],
                          'isError': status >= 400}
                for plugin in plugins:
                    receipt = plugin.observe(event)
                    if hasattr(plugin, 'read_receipt'):
                        source_receipt = plugin.read_receipt(event)
                        if source_receipt is not None:
                            result['content'].append({'type': 'text', 'text': json.dumps(
                                {'scope_read_receipt': source_receipt}, ensure_ascii=False)})
                            plugin.record('read_receipt_exposed', source_call_id=event['call_id'],
                                          result_sha256=source_receipt['result_sha256'])
                    if receipt is not None:
                        visible = {key: receipt[key] for key in (
                            'plugin', 'source_actor', 'source_call_id', 'status', 'persisted',
                            'name', 'raw_sha256', 'error', 'available_prior_versions', 'archive_usage',
                            'archival_evidence', 'archival_versions_omitted', 'failure_layer', 'evidence_manifest', 'source_expansions', 'information_support') if key in receipt}
                        result['content'].append({'type': 'text', 'text': json.dumps(
                            {'runtime_plugin_receipt': visible}, ensure_ascii=False)})
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
            if method == 'tools/call' and any(getattr(plugin, 'version2', False) for plugin in plugins):
                value = {'error': type(exc).__name__ + ': ' + str(exc)}
                for plugin in plugins:
                    if getattr(plugin, 'version2', False) and (
                            params.get('name') in plugin_tools or params.get('name') in plugin.spec['action_tools'] or
                            getattr(plugin, 'recovery_key', '') == 'capability_recovery'):
                        value[getattr(plugin, 'recovery_key', 'scope_recovery')] = plugin.status()
                        plugin.record('tool_rejected', executor_call_id=str(request['id']),
                                      tool=params.get('name'), reason=value['error'])
                response = {'jsonrpc': '2.0', 'id': request['id'], 'result': {
                    'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': True}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    serve(json.loads(Path(sys.argv[1]).read_text()))
