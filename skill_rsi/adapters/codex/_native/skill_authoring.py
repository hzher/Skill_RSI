"""Real skill persistence only for reviewed public authoring tasks."""
import hashlib
import json
from pathlib import Path

from .storage import file_sha
from .vendor.library import DeltaLibrary, quality

CONTRACT = 'public-skill-authoring-v1'


def save_skill(config, args, call_id):
    cap = config.get('skill_write', {})
    if cap.get('contract') != CONTRACT or cap.get('task_id') not in cap.get('authorized_task_ids', []):
        raise ValueError('No public-task-authorized skill authoring capability')
    action = args.get('action')
    required = {'action', 'name', 'content', 'reason'} | ({'expected_sha256'} if action == 'edit' else set())
    if action not in ('create', 'edit') or set(args) != required or not args.get('reason', '').strip():
        raise ValueError('Exact create/edit fields and a reason are required')
    fields = quality(args['content'])
    if fields['name'] != args['name']:
        raise ValueError('The Markdown frontmatter name must match name')
    lib = DeltaLibrary(cap['library'], cap['sequence'])
    state = lib.state()
    parents = []
    if action == 'create':
        if args['name'] in state['active']:
            raise ValueError('Skill already exists; read its current version and edit it')
    else:
        entry = state['active'].get(args['name'])
        if not entry or entry['raw_sha256'] != args['expected_sha256']:
            raise ValueError('Skill version changed or missing; read the current version')
        path = Path(config['events'])
        events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        if not any(e.get('name') == 'Skill' and not e.get('error') and
                   e.get('arguments', {}).get('name') == args['name'] and
                   e.get('result', {}).get('raw_sha256') == args['expected_sha256'] for e in events):
            raise ValueError('A successful complete Skill read is required before editing')
        parents = [args['name']]
    proposal = {'reason': args['reason'], 'operations': [{
        'operation': 'add' if action == 'create' else 'update', 'parents': parents,
        'skills': [{'content': args['content']}], 'evidence_ids': ['public-task'], 'reason': args['reason']}]}
    stage = 'executor-' + cap['task_id'] + '-' + hashlib.sha256(str(call_id).encode()).hexdigest()[:20]
    commit = lib.commit(stage, proposal, file_sha(lib.root / 'state.json'), parents, ['public-task'])
    entry = lib.state()['active'][args['name']]
    content = (lib.root / 'raw_versions' / (entry['raw_sha256'] + '.md')).read_text()
    saved = {**entry, 'description': quality(content)['description'], 'content': content}
    config.setdefault('skills', {})[args['name']] = saved
    return {'persisted': True, 'source_actor': 'executor', 'stage': stage, 'action': action,
            'name': args['name'], 'raw_sha256': saved['raw_sha256'], 'skill': saved, 'commit': commit}
