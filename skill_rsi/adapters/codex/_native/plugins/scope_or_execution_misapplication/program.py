"""Bounded data selection programs over actual public tool results; no eval or I/O."""
import json
import re
from datetime import datetime

from ..common import digest as fingerprint, pointer
from ...storage import atomic, file_sha


class SelectionProgram:
    def __init__(self, rows, resolve, bindings=None):
        self.rows, self.resolve, self.steps = rows, resolve, 0
        self.bindings = bindings or {}

    def run(self, expr, row, depth=0):
        self.steps += 1
        if depth > 24 or self.steps > 50000:
            raise ValueError('Selection program budget exceeded')
        def ev(value, current=row):
            return self.run(value, current, depth + 1)
        if isinstance(expr, list):
            return [ev(value) for value in expr]
        if not isinstance(expr, dict):
            return expr
        if set(expr) == {'field'}:
            try:
                return pointer(row, expr['field'])
            except (KeyError, IndexError, TypeError) as error:
                raise ValueError('Record field ' + repr(expr['field']) + ' is absent or traverses a scalar; '
                                 'use actual record fields ' + repr(sorted(row)) +
                                 '; guard optional fields with {op:"exists",args:["/path"]}') from error
        if set(expr) == {'source'}:
            return self.resolve(expr['source'])
        if set(expr) == {'literal'}:
            return expr['literal']
        if set(expr) == {'var'}:
            return ev(self.bindings[expr['var']])
        if 'op' not in expr:
            return {key: ev(value) for key, value in expr.items()}
        op = expr['op']
        if op == 'exists':
            path = expr.get('args', [])
            if len(path) != 1 or not isinstance(path[0], str):
                raise ValueError('exists needs one literal JSON pointer')
            try:
                pointer(row, path[0])
                return True
            except (KeyError, IndexError, TypeError):
                return False
        if op in ('related', 'distinct_count'):
            # Compare actual fields, never titles or array position as an implicit join.
            key = ev(expr['key'])
            if key is None or key == '':
                return False if op == 'related' else 0
            matches = [other for other in self.rows if ev(expr['key'], other) == key
                       and bool(ev(expr.get('where', True), other))]
            if 'within_seconds' in expr:
                current = datetime.fromisoformat(ev(expr['time']).replace('Z', '+00:00'))
                window = expr['within_seconds']
                if not isinstance(window, (int, float)) or window < 0:
                    raise ValueError('within_seconds must be a nonnegative duration')
                timed = [(datetime.fromisoformat(ev(expr['time'], other).replace('Z', '+00:00')), other)
                         for other in matches]
                # A time window has total width W, not a radius W (which would admit a 2W span).
                groups = [[other for time, other in timed if 0 <= (time-start).total_seconds() <= window]
                          for start, _ in timed if 0 <= (current-start).total_seconds() <= window]
                if op == 'related': return any(groups)
                return max((len({json.dumps(ev(expr['distinct'], other), sort_keys=True) for other in group
                                 if ev(expr['distinct'], other) not in (None, '')}) for group in groups), default=0)
            if op == 'related':
                return bool(matches)
            return len({json.dumps(ev(expr['distinct'], other), sort_keys=True) for other in matches
                        if ev(expr['distinct'], other) not in (None, '')})
        # Short circuit boolean conditions, so optional fields may be guarded.
        if op == 'all':
            return all(bool(ev(value)) for value in expr['args'])
        if op == 'any':
            return any(bool(ev(value)) for value in expr['args'])
        args = [ev(value) for value in expr.get('args', [])]
        if op in ('in', 'contains'):
            container = args[1] if op == 'in' else args[0]
            if not isinstance(container, (str, list, dict)):
                raise ValueError(op + ' requires a string, array or object container; '
                                 'guard null values with all + ne(null) before membership checks')
        if op == 'not': return not args[0]
        if op == 'eq': return args[0] == args[1]
        if op == 'ne': return args[0] != args[1]
        if op == 'in': return args[0] in args[1]
        if op == 'contains': return args[1] in args[0]
        if op == 'ge': return args[0] >= args[1]
        if op == 'length': return len(args[0])
        if op == 'lower': return args[0].lower()
        if op == 'difference': return [value for value in args[0] if value not in args[1]]
        if op == 'intersection': return [value for value in args[0] if value in args[1]]
        if op == 'union':
            result = []
            for collection in args:
                for value in collection:
                    if value not in result: result.append(value)
            return result
        if op == 'section':
            text, start, end = args
            if not isinstance(text, str) or len(text) > 200000:
                raise ValueError('section needs bounded text')
            if start and start not in text:
                raise ValueError('section start marker absent from actual source')
            selected = text.split(start, 1)[1] if start else text
            return selected.split(end, 1)[0] if end else selected
        if op == 'mentioned':
            candidates, text = args
            if not isinstance(candidates, list) or not isinstance(text, str):
                raise ValueError('mentioned needs [candidate-string-array, source-text]')
            return [value for value in candidates if isinstance(value, str) and value
                    and re.search(r'(?<!\w)' + re.escape(value) + r'(?!\w)', text)]
        if op == 'labeled_members':
            candidates, text = args
            if not isinstance(candidates, list) or not isinstance(text, str):
                raise ValueError('labeled_members needs [candidate-string-array, source-text]')
            # Parse assignment/list labels, not every name mentioned in surrounding prose.
            return [value for value in candidates if isinstance(value, str) and value and re.search(
                r'(?m)^\s*(?:(?:[-*]|\d+[.)])\s*)?' + re.escape(value) + r'\s*(?::|—|–| - )', text)]
        if op == 'join': return args[1].join(args[0])
        if op == 'tokens':
            # Token extraction, not arbitrary regex supplied by the model.
            text, required_fragment = args
            return list(dict.fromkeys(token for token in re.findall(r'[\w-]+', text)
                                      if required_fragment in token))
        if op == 'first':
            return args[0][0] if args[0] else None
        raise ValueError('Unsupported selection operator: ' + str(op))


def catalog(spec):
    ref = {'type': 'object', 'properties': {'call_id': {'type': 'string'}, 'pointer': {'type': 'string'}},
           'required': ['call_id', 'pointer'], 'additionalProperties': False}
    return {'name': 'scope_compute', 'description':
        'Compute a full protected-write plan from actual public records, instead of manually choosing IDs. '
        'Copy inventories from scope_read_receipt. records references resolve to full objects or arrays '
        '(read details first); they must cover the entire inventory. policy cites current authoritative '
        'task/rule sources. program={tool,where,arguments,purpose}; tool is an unprefixed protected tool, '
        'The current user task is evidence {call_id:"public-task",pointer:""} (exact hyphenated ID); '
        'use this when the policy is in the task itself. Do not invent read IDs. '
        'where is a predicate per record, arguments maps non-target argument names to expressions; optional '
        'bindings defines reusable expressions referenced as {var:"name"} to avoid repeating eligibility '
        'predicates in distinct_count. The '
        'target ID is inserted by code. Expression constants are plain JSON, {field:"/path"} reads '
        'the current record, {source:{call_id,pointer}} reads real evidence, {literal:value} escapes an '
        'object. Operators use {op,args:[...]}. Supported: all,any,not,eq,ne,in,contains,ge,length,lower, '
        'difference,intersection,union,section,mentioned,labeled_members,join,tokens,first,exists. '
        'exists takes ["/path"] and tests field presence, not non-nullness. Use only fields present in '
        'actual records; guard optional fields with exists and nullable values with ne(null), using '
        'short-circuit all before membership operations. first on an empty array returns null. '
        'section takes [text,exact-start-marker,exact-end-marker]; empty end means through the end. '
        'mentioned takes [candidate names/IDs,text] and returns exact members actually present. '
        'labeled_members takes [candidates,text] and selects only names that label a numbered/bulleted '
        'assignment line (Name: task), excluding prose mentions of observers. Use labeled_members for '
        'action owners; mentioned is for explicitly stated lists, not ownership. join takes [strings,separator]. '
        'For recipient sets, use labeled_members on the action-owner section and subtract the '
        'current exclusion/role list; attendance alone is not task ownership. Use explicit task scope '
        'if it instead authorizes all participants. Do not hardcode names or IDs in program. '
        'related uses {op:"related",key:expression,where:predicate} to require another actual record '
        'sharing the key, e.g. a child with a matching parent type. distinct_count uses '
        '{op:"distinct_count",key:expression,where:predicate,distinct:expression}; optional time:expression '
        'and within_seconds:number restrict to a total-width window containing each record. Filter qualifying facts '
        'before counting; quoted old procedures are not current incident facts. tokens(text,fragment) '
        'is expressed as {op:"tokens",args:[text,fragment]} and extracts matching word/hyphen tokens. '
        'The response contains exact missing_actions; execute them via original business tools, then '
        'read scope_status. A program can be reused by providing program_source referencing the content '
        'of an actual Skill read of a scope-program skill, plus fresh records/inventories/policy; do not '
        'discard a complete saved action template just because current KB is brief. save_program=true '
        'persists a portable template only after all planned writes succeed and only with existing '
        'public skill-authoring permission. Programs using transient source IDs or concrete object IDs '
        'are not persisted. In v7 saving defaults to true under those permissions; set false for a '
        'one-off/nonreusable rule. program_source only accepts a ```scope-program JSON block; if '
        'the read skill is ordinary prose, translate its complete publicly available procedure into '
        'program instead. Never invent missing action fields. Memory/skill editing stay outside this plan. Semantic policy translation '
        'remains unverified; no private answers are consulted.',
        'inputSchema': {'type': 'object', 'properties': {
            'inventories': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'call_id': {'type': 'string'}, 'pointer': {'type': 'string'}, 'id_field': {'type': 'string'}},
                'required': ['call_id', 'pointer', 'id_field'], 'additionalProperties': False}},
            'records': {'type': 'array', 'items': ref}, 'policy': {'type': 'array', 'items': ref},
            'program': {'type': 'object', 'properties': {'tool': {'type': 'string', 'enum': spec['action_tools']},
                'where': {'description': 'Predicate expression; true selects all records.'},
                'arguments': {'type': 'object'}, 'purpose': {'type': 'string'}, 'bindings': {'type': 'object'}},
                'required': ['tool', 'where', 'arguments', 'purpose'], 'additionalProperties': False},
            'program_source': ref, 'save_program': {'type': 'boolean'}},
            'required': ['inventories', 'records', 'policy'], 'additionalProperties': False}}


def compute(guard, args, call_id):
    allowed = {'inventories', 'records', 'policy', 'program', 'program_source', 'save_program'}
    if set(args) - allowed or not args.get('policy') or ('program' in args) == ('program_source' in args):
        raise ValueError('Provide policy evidence and exactly one of program or program_source')
    def resolve(ref): return guard.reference(ref)['resolved_value']
    program = args.get('program')
    if 'program_source' in args:
        ref = args['program_source']
        if not any(e.get('call_id') == ref['call_id'] and e.get('name') == 'Skill'
                   and not e.get('error') for e in guard.events()):
            raise ValueError('program_source requires an actual authorized Skill read')
        content = resolve(ref)
        if not isinstance(content, str):
            raise ValueError('program_source must point to the Skill text /content, not the whole result')
        if '```scope-program\n' not in content or '\n```' not in content.split('```scope-program\n', 1)[1]:
            raise ValueError('This Skill is prose, not an executable scope program. Translate its actual '
                             'complete public procedure into program, citing the Skill in policy; '
                             'retrieve an authorized complete source if action fields are missing')
        program = json.loads(content.split('```scope-program\n', 1)[1].split('\n```', 1)[0])
    required = {'tool', 'where', 'arguments', 'purpose'}
    if not required <= set(program) or set(program) - required - {'bindings'} or program['tool'] not in guard.spec['action_tools']:
        raise ValueError('Invalid program fields or protected tool')
    rows, row_refs = [], []
    for ref in args['records']:
        value = resolve(ref)
        if isinstance(value, dict): value = [value]
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValueError('records must reference actual objects or object arrays')
        rows.extend(value)
        row_refs.extend([ref] * len(value))
    if len(rows) > 500:
        raise ValueError('Record budget exceeded; narrow the public inventory explicitly')
    for ref in args['policy']: guard.reference(ref)
    engine = SelectionProgram(rows, resolve, program.get('bindings'))
    decisions, evaluations = [], []
    target = guard.spec['target_fields'][program['tool']]
    if '/' in target[1:]: raise ValueError('Compiler currently requires a top-level target ID field')
    for row, ref in zip(rows, row_refs):
        identifier = pointer(row, target)
        selected = bool(engine.run(program['where'], row))
        arguments = engine.run(program['arguments'], row) if selected else None
        if selected:
            if not isinstance(arguments, dict) or target[1:] in arguments:
                raise ValueError('Arguments must omit target ID; it is inserted from the real record')
            arguments[target[1:]] = identifier
        decisions.append({'object_id': identifier, 'decision': 'act' if selected else 'skip',
                          'evidence': [*args['policy'], ref], 'actions': [
                              {'tool': program['tool'], 'arguments': arguments}] if selected else []})
        evaluations.append({'object_id': identifier, 'record_sha256': fingerprint(row),
                            'selected': selected, 'computed_arguments': arguments})
    guard.handle('scope_plan', {'inventories': args['inventories'], 'decisions': decisions}, call_id)
    guard.state['computation'] = {'program': program, 'program_sha256': fingerprint(program),
        'policy': [guard.reference(ref) for ref in args['policy']], 'evaluations': evaluations,
        'save_requested': args.get('save_program', guard.spec['kind'] == 'scope-guard-v7'), 'saved': False,
        'source_program_reference': args.get('program_source')}
    atomic(guard.state_path, guard.state)
    guard.record('selection_computed', executor_call_id=call_id, program_sha256=fingerprint(program),
                 selected_ids=[e['object_id'] for e in evaluations if e['selected']], evaluations=evaluations)
    return guard.status()


def compact_program(program):
    """Factor identical expression trees without dropping any policy/action fields."""
    result = json.loads(json.dumps(program))
    bindings = dict(result.get('bindings', {}))
    for index in range(20):
        counts = {}
        def collect(value):
            if isinstance(value, dict):
                if 'literal' in value: return
                if 'op' in value:
                    key = json.dumps(value, sort_keys=True, separators=(',', ':'))
                    if len(key) > 80: counts[key] = counts.get(key, 0) + 1
                for item in value.values(): collect(item)
            elif isinstance(value, list):
                for item in value: collect(item)
        collect(result['where']); collect(result['arguments'])
        candidates = [key for key, count in counts.items() if count > 1]
        if not candidates: break
        key = max(candidates, key=len)
        name = '_shared' + str(index)
        while name in bindings: name += '_'
        def replace(value):
            if isinstance(value, dict):
                if json.dumps(value, sort_keys=True, separators=(',', ':')) == key: return {'var': name}
                if 'literal' in value: return value
                return {k: replace(v) for k, v in value.items()}
            if isinstance(value, list): return [replace(v) for v in value]
            return value
        result['where'] = replace(result['where'])
        result['arguments'] = replace(result['arguments'])
        bindings[name] = json.loads(key)
    if bindings: result['bindings'] = bindings
    return result


def persist_program(guard):
    computation = guard.state.get('computation')
    if not computation or computation['saved'] or not computation['save_requested']:
        return
    cap = guard.config.get('skill_write', {})
    if (not guard.config.get('persistence_flags', {}).get('skills_enabled') or
            cap.get('contract') != 'public-skill-authoring-v1' or cap.get('task_id') not in cap.get('authorized_task_ids', [])):
        return
    if not guard.state['completed'] or not guard.status()['planned_actions_service_accepted']:
        return
    program = computation['program']
    raw = json.dumps(program, ensure_ascii=False, sort_keys=True)
    def transient(value):
        if isinstance(value, dict): return 'source' in value or any(transient(v) for v in value.values())
        if isinstance(value, list): return any(transient(v) for v in value)
        return False
    if transient(program) or any(e['object_id'] in raw for e in computation['evaluations']):
        guard.record('program_not_portable', program_sha256=computation['program_sha256'])
        return
    portable = compact_program(program)
    raw = json.dumps(portable, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    from ...vendor.library import DeltaLibrary, quality
    name = 'scope-program-' + computation['program_sha256'][:20]
    description = 'Executable scope program: ' + program['purpose'][:180]
    content = ('---\nname: ' + name + '\ndescription: ' + json.dumps(description) + '\n---\n\n'
        'Observed public-rule translation with successful service receipts, not semantic certification. '
        'Recheck current policy/version and permissions before reuse. Read this skill, then reference '
        'its /content in scope_compute.program_source with fresh inventories, records and policy.\n\n'
        '```scope-program\n' + raw + '\n```\n')
    quality(content)
    library = DeltaLibrary(cap['library'], cap['sequence'])
    if name not in library.state()['active']:
        proposal = {'reason': 'Preserve complete executed scope program and action fields.', 'operations': [
            {'operation': 'add', 'parents': [], 'skills': [{'content': content}],
             'evidence_ids': ['public-executed-program'], 'reason': 'Reusable computation; no current object IDs.'}]}
        library.commit(name, proposal, file_sha(library.root / 'state.json'), [], ['public-executed-program'])
    entry = library.state()['active'][name]
    saved = {**entry, 'content': content, 'description': description}
    guard.config.setdefault('skills', {})[name] = saved
    computation['saved'] = True
    atomic(guard.state_path, guard.state)
    guard.record('program_persisted', persisted=True, source_actor='framework_plugin',
                 raw_sha256=entry['raw_sha256'], skill=saved, name=name,
                 program_sha256=computation['program_sha256'], portable_program_sha256=fingerprint(portable))
