"""Bounded public index-to-body expansion through existing read-only HTTP tools.

These are explicitly framework-originated calls, never fabricated model calls.
Every call traverses enabled policy hooks and observers and retains its raw result.
"""
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import time
import urllib.error
import urllib.request

from ..common import pointer
from ..schema import schema_issues


def expand(plugin, source_event, *, rules=None, derived_items=None, allow_framework=False):
    from .bridge import failed
    if source_event.get('source_actor') == 'framework_plugin' and not allow_framework: return []
    rules = rules if rules is not None else [r for r in plugin.spec.get('source_expansions', []) if r['source_tool'] == source_event['name']]
    if not rules: return []
    existing = plugin.events()
    used = sum(e.get('source_actor') == 'framework_plugin' and e.get('expansion_plugin') == plugin.spec['kind'] for e in existing)
    remaining = max(0, 8-used)
    tools = {t['name']: t for t in plugin.config['tools']}
    output = []
    for rule in rules:
        target = tools.get(rule['target_tool'])
        if target is None or not target.get('url'): continue
        try: items = derived_items if derived_items is not None else pointer(source_event['result'], rule['items_pointer'])
        except (KeyError, ValueError, TypeError, IndexError): continue
        if not isinstance(items, list): continue
        for index, item in enumerate(items[:2]):
            if not remaining:
                output.append({'status': 'expansion_budget_exhausted', 'next': 'Use the original read tools for any remaining sources.'})
                return output
            try: identity = pointer(item, rule['id_pointer'])
            except (KeyError, ValueError, TypeError, IndexError): continue
            if not isinstance(identity, (str, int)) or isinstance(identity, bool): continue
            args = {rule['argument']: identity}
            if not plugin.is_read(target['name'], args) or target['name'] in plugin.spec['action_tools']: continue
            if schema_issues(args, target['inputSchema']): continue
            if plugin.v8 and any(e.get('name') == target['name'] and e.get('arguments') == args
                                 and not e.get('error') and not failed(e.get('result')) for e in plugin.events()):
                # Only suppress automatic duplicate reads within this episode.
                # Original model reads remain untouched and can refresh any source.
                plugin.record('duplicate_expansion_suppressed', parent_call_id=str(source_event['call_id']),
                              tool=target['name'], arguments=args)
                continue
            suffix = ':evidence-reference:' if derived_items is not None else ':evidence-body:'
            call_id = str(source_event['call_id']) + suffix + str(len(output))
            # Idempotence if an observer is invoked again for the same outer call.
            prior = [e for e in existing if str(e.get('call_id')) == call_id]
            if prior:
                output.append({'status': 'already_observed', 'source_call_id': call_id,
                               'tool': target['name'], 'result': prior[0]['result']})
                continue
            from ..harness import PluginHarness
            harness = PluginHarness(plugin.config)
            started = time.monotonic(); status = 200
            try:
                harness.before(target['name'], args, call_id)
                request = urllib.request.Request(target['url'], data=json.dumps(args).encode(),
                    method=target.get('method', 'POST'), headers={'Content-Type': 'application/json'})
                try:
                    with urllib.request.urlopen(request, timeout=20) as response:
                        status, value = response.status, json.load(response)
                except urllib.error.HTTPError as exc:
                    status, value = exc.code, exc.read().decode(errors='replace')
            except Exception as exc:
                status, value = 500, {'error': type(exc).__name__ + ': ' + str(exc)}
            event = {'type': 'tool', 'name': target['name'], 'arguments': args, 'result': value,
                     'call_id': call_id, 'error': status >= 400, 'response_status': status,
                     'timestamp': datetime.now(timezone.utc).isoformat(), 'endpoint_url': target['url'],
                     'latency_ms': (time.monotonic()-started)*1000,
                     'source_actor': 'framework_plugin', 'expansion_plugin': plugin.spec['kind'],
                     'parent_call_id': str(source_event['call_id'])}
            with Path(plugin.config['events']).open('a') as stream:
                stream.write(json.dumps(event, ensure_ascii=False)+'\n')
            nested_receipts = []
            for observer in harness.plugins:
                receipt = observer.observe(event)
                if receipt is not None:
                    nested_receipts.append({k: v for k, v in receipt.items() if k not in ('skill', 'archival_versions')})
                if hasattr(observer, 'read_receipt'):
                    receipt = observer.read_receipt(event)
                    if receipt is not None: nested_receipts.append(receipt)
            remaining -= 1
            plugin.record('public_source_expanded', source_call_id=call_id,
                          parent_call_id=str(source_event['call_id']), tool=target['name'], error=event['error'])
            output.append({'source_actor': 'framework_plugin', 'source_call_id': call_id,
                           'tool': target['name'], 'arguments': args, 'result': value,
                           'error': event['error'], 'receipts': nested_receipts,
                           'boundary': 'An actual read through an existing tool; not a model-originated invocation or proof of correctness.'})
            if plugin.v8:
                output[-1].pop('boundary')
                output[-1]['receipts'] = [r for r in nested_receipts if r.get('plugin') != plugin.spec['kind']]
    return output


def document_references(text):
    """Extract explicit prose cross-references, never interpret them as instructions."""
    if not isinstance(text, str): return []
    found = []
    for match in re.finditer(r'\b(?:see|refer to|consult)\s+(?:(?:the|full|complete|detailed)\s+)*([^\n.!?]{4,160})', text, re.I):
        title = match.group(1).strip(' \t"\'`')
        if len(title) >= 4 and title not in found: found.append(title)
    return found[:2]


def follow_document_references(plugin, source_event):
    # One extra search/body hop per outer call. Framework observers cannot recurse.
    if source_event.get('source_actor') == 'framework_plugin': return []
    candidates = [source_event] + [e for e in plugin.events()
        if e.get('parent_call_id') == str(source_event['call_id']) and e.get('source_actor') == 'framework_plugin']
    output = []
    for event in candidates:
        if event['name'] != 'kb_get_article' or event.get('error') or not isinstance(event.get('result'), dict): continue
        titles = document_references(event['result'].get('content'))
        seen = {e.get('arguments', {}).get('query') for e in plugin.events() if e.get('name') == 'kb_search'}
        titles = [title for title in titles if title not in seen]
        if not titles: continue
        rule = {'source_tool': 'kb_get_article', 'target_tool': 'kb_search',
                'items_pointer': '/references', 'id_pointer': '/query', 'argument': 'query'}
        searches = expand(plugin, event, rules=[rule], derived_items=[{'query': t} for t in titles], allow_framework=True)
        for search in searches:
            if search.get('error') or not search.get('source_call_id'): continue
            actual = next((e for e in plugin.events() if str(e.get('call_id')) == search['source_call_id']), None)
            if actual:
                search['followup_sources'] = expand(plugin, actual, allow_framework=True)
                plugin.record('document_reference_followed', source_call_id=search['source_call_id'],
                              parent_call_id=str(event['call_id']), query=search.get('arguments', {}).get('query'))
        output.extend(searches)
    return output


def follow_history(plugin, source_event):
    """Follow only visible references through the same authorized session search."""
    if source_event['name'] != 'session_search' or source_event.get('source_actor') == 'framework_plugin': return []
    if not plugin.config.get('sessions') or 'session_search' not in {t['name'] for t in plugin.channels()}: return []
    references = []
    def walk(value):
        if isinstance(value, dict):
            if str(value.get('view', '')).startswith('session reference;'):
                identity = value.get('session_id', value.get('id'))
                if isinstance(identity, str) and identity not in references: references.append(identity)
            for identity in value.get('oversized_session_ids', []):
                if isinstance(identity, str) and identity not in references: references.append(identity)
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(source_event['result'])
    from ... import codex_mcp
    from ..harness import PluginHarness
    existing = plugin.events()
    used = sum(e.get('source_actor') == 'framework_plugin' and e.get('expansion_plugin') == plugin.spec['kind'] for e in existing)
    output = []
    for index, identity in enumerate(references[:min(2, max(0, 8-used))]):
        args = {'query': '', 'session_id': identity, 'limit': 1}
        if not plugin.is_read('session_search', args): continue
        call_id = str(source_event['call_id']) + ':evidence-history:' + str(index)
        if any(str(e.get('call_id')) == call_id for e in existing): continue
        harness = PluginHarness(plugin.config); started = time.monotonic(); status = 200
        try:
            harness.before('session_search', args, call_id)
            value = codex_mcp.search_sessions(plugin.config, args)
            value = codex_mcp.json.loads(codex_mcp.json.dumps(value, ensure_ascii=False))
        except Exception as exc:
            status, value = 500, {'error': type(exc).__name__ + ': ' + str(exc)}
        event = {'type': 'tool', 'name': 'session_search', 'arguments': args, 'result': value,
                 'call_id': call_id, 'error': status >= 400, 'response_status': status,
                 'timestamp': datetime.now(timezone.utc).isoformat(), 'endpoint_url': '',
                 'latency_ms': (time.monotonic()-started)*1000, 'source_actor': 'framework_plugin',
                 'expansion_plugin': plugin.spec['kind'], 'parent_call_id': str(source_event['call_id'])}
        with Path(plugin.config['events']).open('a') as stream: stream.write(json.dumps(event, ensure_ascii=False)+'\n')
        receipts = []
        for observer in harness.plugins:
            receipt = observer.observe(event)
            if receipt is not None: receipts.append({k:v for k,v in receipt.items() if k not in ('skill','archival_versions')})
            if hasattr(observer, 'read_receipt'):
                receipt = observer.read_receipt(event)
                if receipt is not None: receipts.append(receipt)
        plugin.record('authorized_history_reference_expanded', source_call_id=call_id,
                      parent_call_id=str(source_event['call_id']), error=event['error'])
        output.append({'source_actor':'framework_plugin','source_call_id':call_id,'tool':'session_search',
                       'arguments':args,'result':value,'error':event['error'],'receipts':receipts,
                       'boundary':'Exact-ID lookup through the original authorized history channel; nested references remain bounded.'})
    return output
