"""Evidence-bound action plans, enforced before dispatch and audited after returns."""
import json
from pathlib import Path

from ...storage import atomic, canonical
import hashlib


from ..common import digest as fingerprint, pointer


class ScopeGuard:
    def __init__(self, config, spec):
        self.config, self.spec = config, spec
        self.version2 = spec['kind'] in ('scope-guard-v2', 'scope-guard-v3', 'scope-guard-v4', 'scope-guard-v5', 'scope-guard-v6', 'scope-guard-v7')
        self.version3 = spec['kind'] in ('scope-guard-v3', 'scope-guard-v4', 'scope-guard-v5', 'scope-guard-v6', 'scope-guard-v7')
        self.version4 = spec['kind'] in ('scope-guard-v4', 'scope-guard-v5', 'scope-guard-v6', 'scope-guard-v7')
        self.state_path = Path(config['events']).with_name('scope-guard-state.json')
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            'plan': None, 'completed': [], 'pending': None}

    def record(self, status, **fields):
        value = {'plugin': self.spec['kind'], 'source_actor': 'framework_plugin',
                 'status': status, **fields}
        with Path(self.config['plugin_events']).open('a') as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + '\n')
        return value

    def events(self):
        path = Path(self.config['events'])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def read_receipt(self, event):
        """Expose the server ID explicitly; model tool-call IDs are a different namespace."""
        if (not self.version2 or event.get('error') or event['name'] not in self.spec['read_tools']
                or (event['name'] == 'memory' and event.get('arguments', {}).get('action') != 'read')):
            return None
        inventories = []
        def visit(value, path=''):
            if isinstance(value, list):
                for field in sorted(set(self.spec['target_fields'].values())):
                    try:
                        ids = [pointer(item, field) for item in value]
                    except (KeyError, ValueError, TypeError, IndexError):
                        continue
                    if all(isinstance(item, str) and item for item in ids) and len(ids) == len(set(ids)):
                        inventories.append({'call_id': event['call_id'], 'pointer': path,
                                            'id_field': field, 'object_ids': ids})
            elif isinstance(value, dict):
                for key, item in value.items():
                    visit(item, path + '/' + key.replace('~', '~0').replace('/', '~1'))
        if event['name'] in self.spec['inventory_tools']:
            visit(event['result'])
        return {'plugin': self.spec['kind'], 'call_id': event['call_id'], 'tool': event['name'],
                'result_sha256': fingerprint(event['result']), 'result_pointer_base': '',
                'inventories': inventories,
                'reference_help': 'Use this call_id in scope_compute or scope_plan. JSON pointers address the original first '
                                  'content result, not this receipt or the MCP wrapper. Empty pointer means '
                                  'the entire result. id_field is also a JSON pointer, e.g. /id.'}

    def reference(self, ref):
        if self.version3 and set(ref) == {'call_id', 'pointer'}:
            quote = None
        elif set(ref) == {'call_id', 'pointer', 'quote'} and isinstance(ref['quote'], str) and ref['quote'].strip():
            quote = ref['quote']
        else:
            raise ValueError('Each evidence reference needs call_id, pointer and a nonempty exact quote')
        if ref['call_id'] == 'public-task':
            source = self.config['public_task']
        else:
            matches = [event for event in self.events() if event.get('call_id') == ref['call_id']
                       and not event.get('error') and event['name'] in self.spec['read_tools']]
            if len(matches) != 1:
                raise ValueError('Evidence must identify one successful permitted read; copy call_id from '
                                 'scope_read_receipt or scope_status.available_reads. For the current '
                                 'task use {call_id:"public-task",pointer:""}')
            source = matches[0]['result']
        value = pointer(source, ref['pointer'])
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        if quote is not None and quote not in text:
            raise ValueError('Evidence quote does not occur in the actual public source')
        result = {'reference': ref, 'source_sha256': fingerprint(source)}
        if self.version3:
            result.update(resolved_value=value, resolved_sha256=fingerprint(value))
        return result

    def catalog(self):
        catalog = [
            {'name': 'scope_plan', 'description':
             'Before protected writes, submit a complete current-object plan. Inventory references must '
             'cover current public listings. Decide act/skip for every object, cite exact public rules '
             'and object facts, and list exact tool arguments. Semantic rule interpretation remains your '
             'responsibility. Writes outside the accepted plan are blocked. No-op decisions are allowed '
             'only with explicit public evidence. Once any write succeeds the plan is frozen. '
             'Copy call_id and inventory pointers from scope_read_receipt returned with reads, or '
             'from scope_status.available_reads; do not guess model call IDs. Evidence pointers are '
             'RFC6901 paths into the original result (empty string for root); id_field starts with /. '
             'public-task with pointer empty string references the current task.',
             'inputSchema': {'type': 'object', 'properties': {
                 'inventories': {'type': 'array', 'items': {'type': 'object', 'properties': {
                     'call_id': {'type': 'string'}, 'pointer': {'type': 'string'},
                     'id_field': {'type': 'string'}}, 'required': ['call_id', 'pointer', 'id_field'],
                     'additionalProperties': False}},
                 'decisions': {'type': 'array', 'items': {'type': 'object', 'properties': {
                     'object_id': {'type': 'string'}, 'decision': {'type': 'string', 'enum': ['act', 'skip']},
                     'evidence': {'type': 'array', 'items': {'type': 'object', 'properties': {
                         'call_id': {'type': 'string'}, 'pointer': {'type': 'string'}, 'quote': {'type': 'string'}},
                         'required': ['call_id', 'pointer', 'quote'], 'additionalProperties': False}},
                     'actions': {'type': 'array', 'items': {'type': 'object', 'properties': {
                         'tool': {'type': 'string'}, 'arguments': {'type': 'object'}},
                         'required': ['tool', 'arguments'], 'additionalProperties': False}}},
                     'required': ['object_id', 'decision', 'evidence', 'actions'], 'additionalProperties': False}}},
                 'required': ['inventories', 'decisions'], 'additionalProperties': False}},
            {'name': 'scope_status', 'description':
             'Show planned, completed and missing writes. Call before final answer. Service acceptance '
             'does not prove final-state correctness; use original read tools for verification.',
             'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
        ]
        if self.version3:
            plan = catalog[0]
            plan['description'] = (
                'Plan ONLY protected business writes: ' + ', '.join(self.spec['action_tools']) + '. '
                'Do not include memory, skill editing, retrieval, or fictitious memory/skill objects; '
                'invoke those tools normally outside the plan. Copy inventory references from '
                'scope_read_receipt. Each listed object needs one act/skip decision. Evidence needs '
                'only call_id and a JSON pointer into the original result: the server resolves and '
                'seals the actual source value, so do not paraphrase quotes. Empty pointer means root. '
                'Choose actions from the exact tool-name enum; do not use mcp__benchmark__ prefixes. '
                'Use current public rules and object facts to choose targets, recipients and fields. '
                'All planned writes are enforced; once a write succeeds the plan is frozen. '
                'Read scope_status before finishing and verify original business results.')
            decision = plan['inputSchema']['properties']['decisions']['items']['properties']
            decision['evidence']['items']['required'] = ['call_id', 'pointer']
            decision['actions']['items']['properties']['tool']['enum'] = self.spec['action_tools']
        if self.version4:
            from .program import catalog as program_catalog
            catalog[0] = program_catalog(self.spec)
        return catalog

    def handle(self, name, args, call_id):
        if name == 'scope_compute' and self.version4:
            from .program import compute
            return compute(self, args, call_id)
        if name == 'scope_status':
            if args:
                raise ValueError('scope_status takes no arguments')
            result = self.status()
            self.record('status_requested', executor_call_id=call_id, **result)
            return result
        if name != 'scope_plan':
            raise ValueError('Unknown scope tool')
        if self.state['completed'] or self.state['pending']:
            raise ValueError('Cannot replace a plan after dispatch; reconcile preserved receipts')
        if (set(args) != {'inventories', 'decisions'} or not args['inventories']
                or (not args['decisions'] and not self.version2)):
            raise ValueError('A plan requires current inventories and per-object decisions')
        events = self.events()
        objects, inventory_proofs = set(), []
        for ref in args['inventories']:
            if set(ref) != {'call_id', 'pointer', 'id_field'}:
                raise ValueError('Invalid inventory reference')
            matches = [event for event in events if event.get('call_id') == ref['call_id']
                       and not event.get('error') and event['name'] in self.spec['inventory_tools']]
            if len(matches) != 1:
                raise ValueError('Inventory must reference an actual current listing')
            records = pointer(matches[0]['result'], ref['pointer'])
            if not isinstance(records, list) or (not records and not self.version2):
                raise ValueError('Inventory pointer must resolve to a nonempty array')
            identifiers = [pointer(record, ref['id_field']) for record in records]
            if any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
                raise ValueError('Object identifiers must be nonempty strings')
            if len(identifiers) != len(set(identifiers)):
                raise ValueError('Duplicate object identifiers in inventory')
            objects.update(identifiers)
            inventory_proofs.append({'reference': ref, 'source_sha256': fingerprint(matches[0]['result'])})
        decisions = args['decisions']
        if len(decisions) != len(objects) or {decision['object_id'] for decision in decisions} != objects:
            raise ValueError('Every listed object needs exactly one act/skip decision')
        actions, evidence = [], []
        for decision in decisions:
            if set(decision) != {'object_id', 'decision', 'evidence', 'actions'} or not decision['evidence']:
                raise ValueError('Decision requires evidence and exact fields')
            evidence.extend(self.reference(ref) for ref in decision['evidence'])
            if decision['decision'] not in ('act', 'skip'):
                raise ValueError('Unknown decision')
            if bool(decision['actions']) != (decision['decision'] == 'act'):
                raise ValueError('Act needs actions; skip must have none')
            for action in decision['actions']:
                if set(action) != {'tool', 'arguments'} or action['tool'] not in self.spec['action_tools']:
                    raise ValueError('Unbound write tool')
                field = self.spec['target_fields'][action['tool']]
                if pointer(action['arguments'], field) != decision['object_id']:
                    raise ValueError('Action target does not match its object decision')
                actions.append({'id': fingerprint(action), **action, 'object_id': decision['object_id']})
        if len({action['id'] for action in actions}) != len(actions):
            raise ValueError('Duplicate planned action')
        plan = {'sha256': fingerprint(args), 'request': args, 'actions': actions,
                'inventories': inventory_proofs, 'evidence': evidence, 'semantic_validity': 'unverified'}
        self.state['plan'] = plan
        atomic(self.state_path, self.state)
        self.record('plan_accepted', executor_call_id=call_id, plan_sha256=plan['sha256'],
                    object_count=len(objects), action_count=len(actions), semantic_validity='unverified')
        return self.status()

    def before(self, name, args, call_id):
        self.check_before(name,args,call_id)
        self.commit_before(name,args,call_id)

    def check_before(self, name, args, call_id):
        if name not in self.spec['action_tools']:
            return
        action_id = fingerprint({'tool': name, 'arguments': args})
        plan = self.state['plan']
        reason = None
        if not plan:
            reason = ('Submit scope_compute covering the current inventory before writing.' if self.version4
                      else 'Submit scope_plan covering the current inventory before writing.')
        elif self.state['pending']:
            reason = 'A prior dispatch lacks a conclusive receipt; reconcile before retrying.'
        elif action_id not in {action['id'] for action in plan['actions']}:
            reason = 'Arguments are outside the accepted plan. Recheck target, recipients and fields.'
        elif action_id in self.state['completed']:
            reason = 'This exact action already succeeded; do not repeat it.'
        if reason:
            self.record('write_blocked', executor_call_id=call_id, tool=name,
                        arguments_sha256=fingerprint(args), reason=reason)
            raise ValueError('ScopeGuard: ' + reason)

    def commit_before(self, name, args, call_id):
        if name not in self.spec['action_tools']:return
        action_id = fingerprint({'tool': name, 'arguments': args})
        plan = self.state['plan']
        self.state['pending'] = {'action_id': action_id, 'call_id': str(call_id)}
        atomic(self.state_path, self.state)
        self.record('write_dispatched', executor_call_id=call_id, tool=name,
                    action_id=action_id, plan_sha256=plan['sha256'])

    def abort_before(self,name,args,call_id):
        if self.state['pending'] and self.state['pending']['call_id']==str(call_id):
            self.state['pending']=None
            atomic(self.state_path,self.state)
            self.record('dispatch_reservation_cancelled',executor_call_id=str(call_id))

    def observe(self, event):
        pending = self.state['pending']
        if pending and pending['call_id'] == str(event['call_id']):
            if event.get('error') or (isinstance(event['result'], dict) and event['result'].get('error')):
                self.record('write_outcome_unknown', executor_call_id=event['call_id'],
                            action_id=pending['action_id'], result_sha256=fingerprint(event['result']))
            else:
                self.state['completed'].append(pending['action_id'])
                self.state['pending'] = None
                atomic(self.state_path, self.state)
                self.record('write_service_accepted', executor_call_id=event['call_id'],
                            action_id=pending['action_id'], result_sha256=fingerprint(event['result']))
                if self.version4:
                    from .program import persist_program
                    try:
                        persist_program(self)
                    except Exception as error:
                        self.state['computation']['persistence_error'] = type(error).__name__ + ': ' + str(error)
                        atomic(self.state_path, self.state)
                        self.record('program_persistence_failed', error=type(error).__name__ + ': ' + str(error))
        return None

    def status(self):
        plan = self.state['plan']
        missing = [action for action in plan['actions'] if action['id'] not in self.state['completed']] if plan else []
        result = {'plan_sha256': plan['sha256'] if plan else None, 'completed': self.state['completed'],
                'missing_actions': missing, 'pending': self.state['pending'],
                'planned_actions_service_accepted': bool(plan) and not missing and not self.state['pending'],
                'semantic_validity': 'unverified', 'final_state_verified': False}
        if self.version2:
            result['available_reads'] = [receipt for event in self.events()
                                         if (receipt := self.read_receipt(event)) is not None]
        if self.version3:
            result['protected_writes'] = self.spec['target_fields']
            result['plan_scope'] = 'Only inventory objects and protected business writes; memory/skill tools stay outside the plan.'
        if self.version4:
            result['computation'] = self.state.get('computation')
        return result
