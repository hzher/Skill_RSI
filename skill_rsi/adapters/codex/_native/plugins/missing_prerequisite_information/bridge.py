"""Episode-local evidence ledger and action dependency gate over actual public reads.

No task IDs, grader data, hardcoded answers, external I/O, or new access rights.
The model supplies semantic judgments; the plugin verifies provenance and enforces
declared dependencies. Semantic sufficiency remains explicitly unverified.
"""
import json
import re
from copy import deepcopy
from pathlib import Path

from ...storage import atomic
from ..common import digest, pointer

KINDS = ['full_rule', 'field_mapping', 'identity', 'authorization', 'reference_time',
         'destination', 'protocol', 'example_coverage', 'other']


def failed(value):
    if isinstance(value, dict):
        return bool(value.get('error')) or value.get('success') is False or value.get('isError') is True or value.get('status') in ('error', 'failed')
    return False


def leaves(value, path=''):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from leaves(child, path + '/' + str(key).replace('~', '~0').replace('/', '~1'))
    elif isinstance(value, list):
        for i, child in enumerate(value): yield from leaves(child, path + '/' + str(i))
    else: yield path, value


def schema(actions=None, adaptive=False):
    reference = {'type': 'object', 'properties': {'call_id': {'type': 'string'}, 'pointer': {'type': 'string'}},
                 'required': ['call_id', 'pointer'], 'additionalProperties': False}
    requirement = {'type': 'object', 'properties': {
        'id': {'type': 'string'}, 'kind': {'type': 'string', 'enum': KINDS},
        'needed': {'type': 'string'}, 'state': {'type': 'string', 'enum': ['supported', 'missing', 'ambiguous']},
        'sources': {'type': 'array', 'items': reference}, 'reason': {'type': 'string'}},
        'required': ['id', 'kind', 'needed', 'state', 'sources', 'reason'], 'additionalProperties': False}
    action = {'type': 'object', 'properties': {'tool': {'type': 'string'}, 'arguments': {'type': 'object'},
              'requires': {'type': 'array', 'items': {'type': 'string'}}},
              'required': ['tool', 'arguments', 'requires'], 'additionalProperties': False}
    if actions is not None: action['properties']['tool']['enum'] = actions
    return {'type': 'object', 'properties': {'task_quote': {'type': 'string'},
            'requirements': {'type': 'array', 'items': requirement}, 'actions': {'type': 'array', 'items': action}},
            'required': ['requirements', 'actions'] if adaptive else ['task_quote', 'requirements', 'actions'], 'additionalProperties': False}


class EvidenceBridge:
    version2 = True
    recovery_key = 'evidence_recovery'

    def __init__(self, config, spec):
        self.config, self.spec = config, spec
        self.v2 = spec['kind'] in ('evidence-bridge-v2', 'evidence-bridge-v3', 'evidence-bridge-v4', 'evidence-bridge-v5', 'evidence-bridge-v6')
        self.v3 = spec['kind'] in ('evidence-bridge-v3', 'evidence-bridge-v4', 'evidence-bridge-v5', 'evidence-bridge-v6')
        self.v4 = spec['kind'] in ('evidence-bridge-v4', 'evidence-bridge-v5', 'evidence-bridge-v6')
        self.v5 = spec['kind'] in ('evidence-bridge-v5', 'evidence-bridge-v6')
        self.v6 = spec['kind'] == 'evidence-bridge-v6'
        self.v9 = spec['kind'] == 'evidence-bridge-v9'
        self.v8 = spec['kind'] in ('evidence-bridge-v8','evidence-bridge-v9')
        self.v7 = spec['kind'] in ('evidence-bridge-v7','evidence-bridge-v8','evidence-bridge-v9')
        if self.v7:
            self.v2 = self.v3 = self.v4 = self.v5 = self.v6 = True
        if self.v6: self.version2 = False  # Preserve ordinary error response semantics too.
        self.path = Path(config['events']).with_name('evidence-bridge-state.json')

    def state(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {'requirements': {}, 'actions': {}}

    def record(self, status, **fields):
        receipt = {'plugin': self.spec['kind'], 'source_actor': 'framework_plugin', 'status': status, **fields}
        with Path(self.config['plugin_events']).open('a') as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False) + '\n')
        return receipt

    def events(self):
        path = Path(self.config['events'])
        return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []

    def channels(self):
        from ...codex_mcp import base_catalog
        return [t for t in base_catalog(self.config) if t['name'] in self.spec['read_tools']]

    def is_read(self, name, args):
        if name not in self.spec['read_tools']: return False
        variant = self.spec.get('read_variants', {}).get(name)
        if variant:
            try: return pointer(args, variant['pointer']) in variant['values']
            except (KeyError, ValueError, TypeError, IndexError): return False
        return True

    def sources(self):
        available = {t['name'] for t in self.channels()}
        return [e for e in self.events() if e.get('name') in available and self.is_read(e['name'], e.get('arguments', {}))
                and not e.get('error') and not failed(e.get('result'))]

    def resolve(self, ref):
        if ref['call_id'] == 'public-task':
            source = self.config.get('public_task', '')
        else:
            matches = [e for e in self.sources() if str(e.get('call_id')) == ref['call_id']]
            if len(matches) != 1: raise ValueError('Reference must name one successful currently permitted read')
            source = matches[0]['result']
            # A newer successful read of the same request supersedes an older observation.
            same = [e for e in self.sources() if e['name'] == matches[0]['name'] and e.get('arguments') == matches[0].get('arguments')]
            if same[-1].get('call_id') != matches[0].get('call_id'):
                raise ValueError('Source was superseded; inspect and cite the latest read')
        value = pointer(source, ref['pointer'])
        if value is None or value == '' or value == [] or value == {} or failed(value):
            raise ValueError('Empty or failed source cannot support a requirement')
        return {'reference': ref, 'source_sha256': digest(source), 'value_sha256': digest(value), 'value': value}

    def inspect(self, query=''):
        tokens = set(re.findall(r'\w+', query.lower()))
        found = []
        for event in self.sources():
            for path, value in leaves(event['result']):
                if not isinstance(value, str) or not value.strip(): continue
                score = sum(t in value.lower() for t in tokens)
                if tokens and not score: continue
                hints = []
                if path.endswith('/snippet'): hints.append('search_snippet_not_full_source')
                if re.search(r'\b(see|refer to|consult)\b.{0,100}\b(full|complete)\b|\b(overview|brief)\b', value, re.I):
                    hints.append('possible_summary_or_reference_follow_original_source')
                found.append({'call_id': str(event['call_id']), 'pointer': path, 'tool': event['name'],
                              'preview': value[:900], 'truncated': len(value) > 900,
                              'value_sha256': digest(value), 'hints': hints, 'score': score})
        found.sort(key=lambda item: -item['score'])
        return {'matches': found[:20], 'matches_omitted': max(0, len(found)-20),
                'read_channels': self.channels(), 'read_variants': self.spec.get('read_variants', {}),
                'available_skill_names': list(self.config.get('skills', {})),
                'boundary': 'Only already observed sources are searched. Empty results do not establish global absence. '
                            'Previews are not complete evidence; use original reads or evidence_plan references.',
                'semantic_sufficiency': 'unverified'}

    def status(self):
        state = self.state()
        return {'requirements': state['requirements'], 'actions': state['actions'],
                'available_read_tools': [t['name'] for t in self.channels()], 'semantic_sufficiency': 'unverified'}

    def catalog(self):
        if self.v5: return []
        plan_description = ('Register missing/ambiguous/supported prerequisites and exact intended actions. Cite actual read call IDs and JSON pointers or public-task with empty pointer. Each action requires IDs of supported prerequisites. Unrelated gaps may remain. This records a plan, not execution; use original tools after acceptance. No semantic correctness guarantee.')
        if self.v3:
            plan_description = ('Track a real information gap and exact action dependencies. Cite actual permitted read call IDs and JSON pointers. '
                                'Requirements may be supported, missing or ambiguous. The plan returns ready_actions and blocked_actions; '
                                'execute only ready_actions using original tools. The current task is bound automatically. '
                                'No new retrieval, business execution or semantic certification occurs.')
        return [{'name': 'evidence_inspect', 'description': 'Search already observed public read results; recover exact server call IDs and JSON pointers, discover current read channels and source-completeness hints. No new retrieval or business write occurs.',
                 'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'additionalProperties': False}},
                {'name': 'evidence_plan', 'description': plan_description,
                 'inputSchema': schema(self.action_tools() if self.v2 else None, self.v3)}]

    def action_tools(self):
        from ...codex_mcp import base_catalog
        available = {t['name'] for t in base_catalog(self.config)}
        return [name for name in self.spec['action_tools'] if name in available]

    def handle(self, name, args, call_id):
        if name == 'evidence_inspect':
            if set(args)-{'query'} or not isinstance(args.get('query', ''), str): raise ValueError('Invalid evidence query')
            result = self.inspect(args.get('query', ''))
            self.record('evidence_inspected', executor_call_id=str(call_id), matched=len(result['matches']))
            return result
        if name != 'evidence_plan': raise ValueError('Unknown evidence tool')
        try:
            result = self.plan(args, call_id)
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            self.record('evidence_plan_rejected', executor_call_id=str(call_id), reason=str(exc))
            return {'accepted': False, 'operation_executed': False, 'issue': str(exc)}
        return result

    def plan(self, args, call_id):
        from ..schema import schema_issues
        if self.v2:
            args = deepcopy(args)
            for action in args.get('actions', []) if isinstance(args, dict) else []:
                if isinstance(action, dict) and isinstance(action.get('tool'), str) and action['tool'].startswith('mcp__benchmark__'):
                    action['tool'] = action['tool'][len('mcp__benchmark__'):]
        issues = schema_issues(args, schema(self.action_tools() if self.v2 else None, self.v3))
        if issues: raise ValueError('; '.join(issues))
        quote, task = args.get('task_quote', self.config.get('public_task', '')), self.config.get('public_task', '')
        if self.v2: quote, task = ' '.join(quote.split()), ' '.join(task.split())
        if not self.v3 and (not quote.strip() or quote not in task):
            raise ValueError('task_quote must occur in the actual current task')
        if not 1 <= len(args['requirements']) <= 32 or len(args['actions']) > 100:
            raise ValueError('Use 1..32 requirements and at most 100 actions')
        requirements = {}
        for req in args['requirements']:
            if not req['id'].strip() or req['id'] in requirements or not req['needed'].strip() or not req['reason'].strip():
                raise ValueError('Unique requirement IDs, needed information and rationale are required')
            proofs = [self.resolve(ref) for ref in req['sources']]
            if req['state'] == 'supported' and not proofs: raise ValueError('Supported requirements need actual evidence')
            if req['state'] == 'supported' and req['kind'] in ('full_rule', 'protocol'):
                if all(p['reference']['pointer'].endswith('/snippet') for p in proofs):
                    raise ValueError('A search snippet alone is not a complete rule/protocol; fetch its body')
            requirements[req['id']] = {**req, 'proofs': proofs, 'semantic_sufficiency': 'model_asserted_unverified'}
        state = self.state()
        if not self.v3 and any(a.get('pending') for a in state['actions'].values()): raise ValueError('Reconcile unresolved dispatch before replacing plan')
        actions = {}
        for action in args['actions']:
            if action['tool'] not in self.spec['action_tools']: raise ValueError('Only configured business actions may be planned')
            if self.is_read(action['tool'], action['arguments']): raise ValueError('Read variants do not need action preparation')
            if not action['requires'] or any(k not in requirements or (not self.v3 and requirements[k]['state'] != 'supported') for k in action['requires']):
                raise ValueError('Every action needs supported dependencies; omit actions that depend on missing/ambiguous facts')
            key = digest({'tool': action['tool'], 'arguments': action['arguments']})
            if key in actions: raise ValueError('Duplicate action')
            prior = state['actions'].get(key, {})
            if prior.get('pending'): raise ValueError('Cannot replace the exact action with an unresolved dispatch')
            actions[key] = {**action, 'pending': None, 'completed': prior.get('completed', False)}
        # Preserve completed history so re-planning cannot replay an already accepted action.
        for key, old in state['actions'].items():
            if (old.get('completed') or old.get('pending')) and key not in actions: actions[key] = old
        atomic(self.path, {'requirements': requirements, 'actions': actions,
                           **({'task_sha256': digest(self.config.get('public_task', ''))} if self.v3 else {})})
        self.record('evidence_plan_accepted', executor_call_id=str(call_id), requirements=requirements, actions=actions)
        readiness = {'ready_actions': [a for a in args['actions'] if all(requirements[k]['state'] == 'supported' for k in a['requires'])],
                     'blocked_actions': [a for a in args['actions'] if any(requirements[k]['state'] != 'supported' for k in a['requires'])]} if self.v3 else {}
        return {'accepted': True, 'operation_executed': False, 'action_count': len(args['actions']), **readiness,
                'unresolved': [k for k, r in requirements.items() if r['state'] != 'supported'],
                'semantic_sufficiency': 'unverified'}

    def check_before(self, name, args, call_id):
        if name not in self.spec['action_tools'] or self.is_read(name, args): return
        state = self.state(); action = state['actions'].get(digest({'tool': name, 'arguments': args}))
        if self.v3 and not action:
            self.record('unassessed_action_check', executor_call_id=str(call_id), tool=name,
                        certification=False)
            return
        try:
            if not action: raise ValueError('Use evidence_plan with supported prerequisites before this exact write')
            if action.get('completed') or action.get('pending'): raise ValueError('Action completed or dispatch unresolved; do not replay')
            for key in action['requires']:
                req = state['requirements'][key]
                if req['state'] != 'supported': raise ValueError('Unresolved prerequisite')
                for proof in req['proofs']:
                    if self.resolve(proof['reference'])['source_sha256'] != proof['source_sha256']:
                        raise ValueError('Source changed; reassess prerequisites')
        except (ValueError, KeyError) as exc:
            self.record('unsupported_action_blocked', executor_call_id=str(call_id), tool=name, reason=str(exc))
            raise ValueError('EvidenceBridge: ' + str(exc)) from exc

    def commit_before(self, name, args, call_id):
        if name not in self.spec['action_tools'] or self.is_read(name, args): return
        state = self.state(); key = digest({'tool': name, 'arguments': args})
        if self.v3 and key not in state['actions']:
            self.record('unassessed_dispatch', executor_call_id=str(call_id), action_sha256=key, certification=False)
            return
        state['actions'][key]['pending'] = str(call_id); atomic(self.path, state)
        self.record('evidence_bound_dispatch', executor_call_id=str(call_id), action_sha256=key)

    def abort_before(self, name, args, call_id):
        state = self.state(); key = digest({'tool': name, 'arguments': args})
        if state['actions'].get(key, {}).get('pending') == str(call_id):
            state['actions'][key]['pending'] = None; atomic(self.path, state)
            self.record('dispatch_reservation_cancelled', executor_call_id=str(call_id))

    def observe(self, event):
        if event['name'] in self.spec['action_tools'] and not self.is_read(event['name'], event.get('arguments', {})):
            state = self.state(); key = digest({'tool': event['name'], 'arguments': event['arguments']})
            action = state['actions'].get(key)
            if action and action.get('pending') == str(event['call_id']):
                success = not event.get('error') and not failed(event.get('result'))
                if success: action.update(completed=True, pending=None)
                atomic(self.path, state)
                self.record('action_service_accepted' if success else 'action_unresolved',
                            executor_call_id=str(event['call_id']), action_sha256=key, semantic_success='unverified')
            elif self.v3 and action is None:
                self.record('unassessed_action_return', executor_call_id=str(event['call_id']),
                            action_sha256=key, error=bool(event.get('error') or failed(event.get('result'))), certification=False)
        if not self.is_read(event['name'], event.get('arguments', {})): return None
        if event.get('error') or failed(event.get('result')):
            receipt = self.record('source_failed', source_call_id=str(event['call_id']), persisted=False)
            return None if self.v5 else receipt
        manifest = [{'pointer': p, 'type': type(v).__name__} for p, v in leaves(event['result'])][:40]
        receipt = self.record('evidence_available', source_call_id=str(event['call_id']), persisted=False,
                              evidence_manifest=manifest, raw_sha256=digest(event['result']))
        if self.v4:
            from .recovery import expand
            expansions = expand(self, event)
            if self.v6 and self.spec.get('follow_history_references'):
                from .recovery import follow_history
                expansions += follow_history(self, event)
            if self.v7 and self.spec.get('follow_document_references'):
                from .recovery import follow_document_references
                expansions += follow_document_references(self, event)
            if expansions: receipt['source_expansions'] = expansions
        if self.v5 and not receipt.get('source_expansions'): return None
        if self.v8:
            # Plan tools are disabled in code-only versions. Keep the full index
            # in the audit log, but expose only provenance and actual new bodies.
            receipt.pop('evidence_manifest', None)
        return receipt
