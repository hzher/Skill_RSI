"""Native stdio MCP proposal tool. Only serializes the model's original arguments."""
import json
import copy
import re
import sys
from pathlib import Path


def schema(*, enforce_shapes=False):
    text = {'type': 'string', 'minLength': 1}
    skill = {'type': 'object', 'properties': {
        'name': {'type': 'string', 'pattern': '^[a-z][a-z0-9-]{2,63}$'},
        'description': {'type': 'string', 'minLength': 4, 'maxLength': 350},
        'applicability': {'type': 'string', 'minLength': 4, 'maxLength': 350},
        'exclusions': {'type': 'string', 'minLength': 4, 'maxLength': 350},
        'steps': {'type': 'array', 'items': {'type': 'string', 'minLength': 4, 'maxLength': 350}, 'minItems': 2, 'maxItems': 10},
        'validation': {'type': 'array', 'items': {'type': 'string', 'minLength': 4, 'maxLength': 350}, 'minItems': 1, 'maxItems': 6},
        'dependencies': {'type': 'array', 'items': {'type': 'string', 'pattern': '^[a-z][a-z0-9-]{2,63}$'}, 'maxItems': 4}},
        'required': ['name', 'description', 'applicability', 'exclusions', 'steps', 'validation', 'dependencies'], 'additionalProperties': False}
    delta = {'type': 'object', 'properties': {
        'operation': {'type': 'string', 'enum': ['add', 'update', 'merge', 'split', 'archive']},
        'parents': {'type': 'array', 'items': dict(text), 'maxItems': 8},
        'skills': {'type': 'array', 'items': skill, 'maxItems': 4},
        'evidence_ids': {'type': 'array', 'items': dict(text), 'minItems': 1, 'maxItems': 12},
        'reason': dict(text)}, 'required': ['operation', 'parents', 'skills', 'evidence_ids', 'reason'], 'additionalProperties': False}
    item = delta
    if enforce_shapes:
        variants = []
        for operation, counts in [('add', (0, 0, 1, 1)), ('update', (1, 1, 1, 1)),
                                  ('merge', (2, 8, 1, 1)), ('split', (1, 1, 2, 4)),
                                  ('archive', (1, 8, 0, 0))]:
            variant = copy.deepcopy(delta)
            properties = variant['properties']
            properties['operation']['enum'] = [operation]
            properties['parents'].update(minItems=counts[0], maxItems=counts[1])
            properties['skills'].update(minItems=counts[2], maxItems=counts[3])
            variants.append(variant)
        item = {'anyOf': variants}
    return {'type': 'object', 'properties': {'reason': dict(text),
            'operations': {'type': 'array', 'items': item, 'maxItems': 8}},
            'required': ['reason', 'operations'], 'additionalProperties': False}


def render_proposal(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {'reason', 'operations'}:
        raise ValueError('Exact reason and operations required')
    operations = []
    for delta in arguments['operations']:
        children = []
        for skill in delta['skills']:
            if set(skill) != {'name', 'description', 'applicability', 'exclusions', 'steps', 'validation', 'dependencies'}:
                raise ValueError('Exact structured skill fields required')
            body = ('---\nname: ' + skill['name'] + '\ndescription: ' + json.dumps(skill['description'], ensure_ascii=False)
                    + '\n---\n## 适用条件\n' + skill['applicability'] + '\n## 排除条件\n' + skill['exclusions']
                    + '\n## 操作步骤\n' + '\n'.join(str(i + 1) + '. ' + step for i, step in enumerate(skill['steps']))
                    + '\n## 验证\n' + '\n'.join('- ' + item for item in skill['validation'])
                    + '\n组合依赖 ' + json.dumps(skill['dependencies'], ensure_ascii=False) + '\n')
            children.append({'content': body})
        operations.append({**delta, 'skills': children})
    return {'reason': arguments['reason'], 'operations': operations}


def submit(arguments, path):
    path = Path(path)
    policy_path = path.with_name('SUBMISSION_ALLOWED.json')
    if policy_path.exists():
        policy = json.loads(policy_path.read_text())
        for delta in arguments.get('operations', []):
            citations = delta.get('evidence_ids')
            if not isinstance(citations, list) or not citations or not set(citations) <= set(policy['evidence_ids']):
                raise ValueError('Use only actual successful Read IDs ' + repr(policy['evidence_ids'])
                                 + '; received ' + repr(citations) + '. Do not cite a merely listed event.')
            parents = delta.get('parents')
            if not isinstance(parents, list) or not set(parents) <= set(policy['parent_names']):
                raise ValueError('Existing fully Read skill parents are ' + repr(policy['parent_names'])
                                 + '; source task labels are not skill parents.')
            for child in delta.get('skills', []):
                name = child.get('name', '')
                if re.fullmatch(r'e[0-9]+-(s|d|t[0-9]+|n)', name) and not (delta.get('operation') == 'update' and name in parents):
                    raise ValueError('New skill name must describe a reusable business stage, not source task label ' + name)
    elif str(path) == '/workspace/transaction.json':
        raise ValueError('Current native Read policy absent, no proposal accepted')
    proposal = render_proposal(arguments)
    raw = json.dumps(arguments, ensure_ascii=False, indent=2, allow_nan=False).encode() + b'\n'
    if len(raw) > 70000:
        raise ValueError('Proposal byte envelope exceeded')
    if path.is_symlink():
        raise ValueError('Proposal symlink rejected')
    # Canonical serialization is explicit protocol behavior, with no added fields or content repair.
    path.with_name('transaction-arguments.json').write_bytes(raw)
    path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    return {'saved': str(path), 'committed': False, 'original_arguments_saved': True,
            'rendering': 'Fixed headings/frontmatter only; every business field comes from the model'}


def handle(request, path='/workspace/transaction.json'):
    method = request.get('method')
    if method == 'initialize':
        return {'protocolVersion': request.get('params', {}).get('protocolVersion', '2024-11-05'),
                'capabilities': {'tools': {}}, 'serverInfo': {'name': 'skill-library-proposal', 'version': '2.0'}}
    if method == 'tools/list':
        return {'tools': [{'name': 'submit_transaction',
                 'description': 'Submit zero-to-many skill deltas. Child fields name/description/applicability/exclusions/steps/validation/dependencies are model-authored. A fixed renderer creates Markdown and saves original arguments separately; no business content is added. Cite only the exact allowed fully Read event IDs. This saves a proposal without committing the library.',
                 'inputSchema': schema()}]}
    if method == 'tools/call':
        parameters = request.get('params', {})
        if parameters.get('name') != 'submit_transaction':
            raise ValueError('Unknown proposal tool')
        result = submit(parameters.get('arguments'), path)
        return {'content': [{'type': 'text', 'text': json.dumps(result)}], 'isError': False}
    if method == 'ping':
        return {}
    if method and method.startswith('notifications/'):
        return None
    raise ValueError('Unsupported MCP method')


def main():
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        try:
            result = handle(request)
            response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
        except Exception as error:
            response = {'jsonrpc': '2.0', 'id': request['id'],
                        'error': {'code': -32602, 'message': str(error)}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
