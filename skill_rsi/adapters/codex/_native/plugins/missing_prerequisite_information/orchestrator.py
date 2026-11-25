"""Bounded parent router over three information strategies and one evidence ledger."""
import json
from pathlib import Path
from .bridge import EvidenceBridge, failed, schema
from .subplugins import CHILDREN
from ..common import digest
from ...storage import atomic


class InformationOrchestrator(EvidenceBridge):
    def __init__(self, config, spec):
        # Reuse v9 provenance, dependency and deduplicated retrieval semantics.
        # The exposed identity remains the parent, including framework read calls.
        super().__init__(config, {**spec, 'kind': 'evidence-bridge-v9'})
        self.spec = spec
        self.variant2 = spec['kind'] in ('information-orchestrator-v2','information-orchestrator-v3','information-orchestrator-v4','information-orchestrator-v5')
        self.variant3 = spec['kind'] in ('information-orchestrator-v3','information-orchestrator-v4','information-orchestrator-v5')
        self.variant4 = spec['kind'] in ('information-orchestrator-v4','information-orchestrator-v5')
        self.variant5 = spec['kind']=='information-orchestrator-v5'
        self.version2 = False
        self.path = Path(config['events']).with_name('information-evidence-state.json')
        self.router_path = Path(config['events']).with_name('information-router-state.json')
        self.field_path = Path(config['events']).with_name('information-field-plans.json')

    def routing(self):
        return json.loads(self.router_path.read_text()) if self.router_path.exists() else {'selected': [], 'decisions': []}

    def route(self, event=None, requested=None):
        state = self.routing()
        selection = self.spec['selection']
        limit = selection['max_active']
        pool = self.spec['subplugins']
        if not limit:
            return []
        candidates = []
        for name in pool:
            score, reason = CHILDREN[name].probe(self, event)
            if selection['mode'] == 'manual':
                score, reason = 5, 'manual_selection'
            if name == requested:
                score, reason = 6, 'executor_requested_assessment'
            if score:
                candidates.append((name, score, reason))
        candidates.sort(key=lambda item: (-item[1], pool.index(item[0])))
        changed = False
        for name, score, reason in candidates:
            if name in state['selected']:
                continue
            status = 'selected' if len(state['selected']) < limit else 'deferred_by_cap'
            decision = {'child': name, 'status': status, 'reason': reason,
                        'source_call_id': str(event['call_id']) if event else 'public-task'}
            if decision in state['decisions']:
                continue
            state['decisions'].append(decision)
            if status == 'selected':
                state['selected'].append(name)
            self.record('subplugin_'+status, **{k:v for k,v in decision.items() if k != 'status'})
            changed = True
        if changed:
            atomic(self.router_path, state)
        return state['selected']

    def initial_context(self):
        selected = self.route()
        if not selected:
            return ''
        return '\n\nInformation support:\n' + '\n'.join(
            CHILDREN[n].guidance_for(self) if hasattr(CHILDREN[n],'guidance_for') else CHILDREN[n].guidance
            for n in selected)

    def catalog(self):
        if not self.spec['selection']['max_active']:
            return []
        tools = [{'name': 'information_sources', 'description': 'Inspect actual already observed sources and currently authorized read channels; no new retrieval or semantic certification.',
                  'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'additionalProperties': False}}]
        if 'context_requirements' in self.spec['subplugins']:
            assessment_schema=schema(self.action_tools(), adaptive=True)
            if self.variant4:
                from .partial import assessment_schema as field_schema
                assessment_schema=field_schema(self)
            elif self.variant3:
                assessment_schema['properties']['requirements']['items']['properties']['kind']['enum']=[
                    'identity','authorization','reference_time','destination','other']
            tools.append({'name': 'information_assess',
                          'description': 'Optional: register a real unresolved prerequisite and exact dependent actions. Cite successful read call IDs/JSON pointers. Missing or ambiguous dependencies block only the registered actions. Supported work needs no plan. This does not execute an action or prove semantic correctness.',
                          'inputSchema': assessment_schema})
        return tools

    def handle(self, name, args, call_id):
        if name == 'information_sources':
            if set(args)-{'query'} or not isinstance(args.get('query',''),str):
                raise ValueError('Invalid source query')
            return self.inspect(args.get('query',''))
        if name != 'information_assess':
            raise ValueError('Unknown information support tool')
        if 'context_requirements' not in self.route(requested='context_requirements'):
            return {'accepted': False, 'status': 'deferred_by_cap', 'operation_executed': False,
                    'issue': 'Context assessment is not active; this tool has not certified any action.'}
        if self.variant3 and not self.variant4 and any(r.get('kind') in ('full_rule','field_mapping','protocol','example_coverage')
                                 for r in args.get('requirements',[])):
            self.record('procedure_gap_not_context_gate', executor_call_id=str(call_id))
            return {'accepted':False,'operation_executed':False,
                    'issue':'This tool checks concrete context prerequisites. A missing complete procedure does not by itself prove that ordinary delegated judgments are forbidden. Use the available public procedure and original task, keeping unsupported exact mappings explicit; respect tasks that expressly require a particular full procedure.'}
        try:
            if not self.variant4:
                return self.plan(args, call_id)
            from .partial import compile_fields, assessment_schema
            from ..schema import schema_issues
            from copy import deepcopy
            args=deepcopy(args)
            for action in args.get('actions',[]):
                if isinstance(action.get('tool'),str) and action['tool'].startswith('mcp__benchmark__'):
                    action['tool']=action['tool'][len('mcp__benchmark__'):]
            issues=schema_issues(args,assessment_schema(self))
            if issues:raise ValueError('; '.join(issues))
            requirements={r['id']:r for r in args['requirements']}
            procedural={'full_rule','field_mapping','protocol','example_coverage'}
            if any(requirements.get(n,{}).get('kind') in procedural
                   for a in args['actions'] for n in a['requires']):
                raise ValueError('Procedure gaps must be scoped to particular fields, not a new global prerequisite')
            compiled,fields=compile_fields(self,args)
            result=self.plan(compiled,call_id)
            existing=json.loads(self.field_path.read_text()) if self.field_path.exists() else []
            for field in fields:
                existing=[r for r in existing if (r['tool'],r['identity'])!=(field['tool'],field['identity'])]
                existing.append(field)
            atomic(self.field_path,existing)
            result['field_compilation']=fields
            self.record('field_dependencies_compiled',executor_call_id=str(call_id),fields=fields)
            return result
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            self.record('information_assessment_rejected', executor_call_id=str(call_id), reason=str(exc))
            return {'accepted': False, 'operation_executed': False, 'issue': str(exc)}

    def check_before(self, name, args, call_id):
        if not self.spec['selection']['max_active']:
            return
        if self.variant4 and self.field_path.exists():
            for field in json.loads(self.field_path.read_text()):
                if (field['tool']==name and all(args.get(k)==v for k,v in field['identity'].items())
                        and any(k in args for k in field['omitted_fields'])):
                    raise ValueError('An unresolved field was reintroduced; reassess its actual evidence or use the compiled partial arguments')
        if self.spec['selection']['max_active']:
            super().check_before(name, args, call_id)

    def commit_before(self, name, args, call_id):
        if self.spec['selection']['max_active']:
            super().commit_before(name, args, call_id)

    def observe(self, event):
        if not self.spec['selection']['max_active']:
            return None
        selected = self.route(event)
        if not self.is_read(event['name'], event.get('arguments', {})):
            # Base class handles real pending action receipts; it performs no
            # read expansion on business actions.
            return super().observe(event)
        if event.get('error') or failed(event['result']):
            self.record('source_failed', source_call_id=str(event['call_id']))
            return None
        expanded, support = [], []
        for child in selected:
            additions, observations = CHILDREN[child].observe(self, event)
            expanded.extend(additions)
            support.extend({'child': child, **value} for value in observations)
        self.record('information_source_observed', source_call_id=str(event['call_id']),
                    raw_sha256=digest(event['result']), selected_children=selected)
        if not expanded and not support:
            return None
        return {'plugin': self.spec['kind'], 'source_actor': 'framework_plugin',
                'source_call_id': str(event['call_id']), 'source_expansions': expanded,
                'information_support': support}
