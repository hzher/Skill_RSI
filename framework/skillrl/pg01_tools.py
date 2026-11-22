"""Opt-in PG01 support tools; original history requirements and scoring stay intact."""
import hashlib
import json
from pathlib import Path

from .storage import file_sha
from .vendor.library import DeltaLibrary

CONTRACT = 'pg01-tools-evidence-v1'
PG02_CONTRACT = 'pg02-snapshot-tools-evidence-v1'
PG03_CONTRACT = 'pg03-tools-state-evidence-v1'
PG04_CONTRACT = 'pg04-public-asof-tools-v1'
PG05_CONTRACT = 'pg05-capability-preserving-v1'
PG06_CONTRACT = 'pg06-capability-preserving-v1'
WRITE_TASKS = {CONTRACT: 'PG01_UPDATE_001', PG02_CONTRACT: 'PG02_UPDATE_001', PG03_CONTRACT: 'PG03_UPDATE_001', PG04_CONTRACT: 'PG04_UPDATE_001', PG05_CONTRACT: 'PG05_UPDATE_001', PG06_CONTRACT: 'PG06_UPDATE_001'}
READ_STAGES = frozenset(('PG01_EVAL_NEAR_A_001', 'PG01_EVAL_NEAR_B_001', 'PG01_EVAL_FAR_001',
                         'PG01_CONTROL_PRELOAD_001', 'PG01_CONTROL_BOUNDARY_001'))
PG03_READ_STAGES = frozenset(('PG03_EVAL_NEAR_A_001','PG03_EVAL_NEAR_B_001','PG03_EVAL_FAR_001',
                             'PG03_CONTROL_PRELOAD_001','PG03_CONTROL_BOUNDARY_001'))
PG05_READ_STAGES = frozenset(('PG05_EVAL_NEAR_A_001','PG05_EVAL_NEAR_B_001','PG05_EVAL_FAR_001',
                             'PG05_CONTROL_PRELOAD_001','PG05_CONTROL_BOUNDARY_001'))


def tool_flags(flags, *, task_id, contract, persistence_allowed):
    result = dict(flags)
    # Collection profile retains skill access for evaluation, while control
    # permissions remain exactly those resolved by the original benchmark.
    if '_CONTROL_' in task_id:
        return result
    if contract == CONTRACT and task_id in READ_STAGES and persistence_allowed:
        # Reading a general procedure supports, and never substitutes for, history lookup.
        result['skills_enabled'] = True
    if contract == PG03_CONTRACT and task_id in PG03_READ_STAGES and persistence_allowed:
        result['skills_enabled'] = True
    if contract == PG05_CONTRACT and task_id in PG05_READ_STAGES and persistence_allowed:
        result['skills_enabled'] = True
    return result


def current_skill(config, name):
    if name not in config.get('skills', {}):
        raise ValueError('Skill outside the provided catalog')
    capability = config.get('skill_write')
    if not capability:
        return config['skills'][name]
    library = DeltaLibrary(capability['library'], capability['sequence'])
    entry = library.state()['active'][name]
    content = (library.root / 'raw_versions' / (entry['raw_sha256'] + '.md')).read_text()
    from .vendor.library_base import parse_skill
    return {**entry, 'content': content, 'description': parse_skill(content)[0]['description']}


def edit_skill(config, args, call_id):
    capability = config.get('skill_write')
    if not capability or capability.get('task_id') != WRITE_TASKS.get(capability.get('contract')) or capability.get('contract') not in WRITE_TASKS:
        raise ValueError('No task-authorized skill editing capability')
    if set(args) != {'action', 'name', 'content', 'expected_sha256', 'reason'} or args['action'] != 'edit':
        raise ValueError('An edit with name, content, expected_sha256 and reason is required')
    name = args['name']
    current = current_skill(config, name)
    if current['raw_sha256'] != args['expected_sha256']:
        raise ValueError('Skill version changed; read its current complete content first')
    path = Path(config['events'])
    events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    if not any(e.get('name') == 'Skill' and not e.get('error') and
               e.get('arguments', {}).get('name') == name and
               e.get('result', {}).get('raw_sha256') == args['expected_sha256'] for e in events):
        raise ValueError('A successful full Skill read of this version is required')
    library = DeltaLibrary(capability['library'], capability['sequence'])
    proposal = {'reason': args['reason'], 'operations': [{'operation': 'update', 'parents': [name],
        'skills': [{'content': args['content']}], 'evidence_ids': ['public-task'], 'reason': args['reason']}]}
    stage = 'executor-' + capability['task_id'] + '-' + hashlib.sha256(str(call_id).encode()).hexdigest()[:20]
    # public-task is the actual request already delivered to the actor, not a fabricated Read.
    # Changed-parent read is independently checked against the actual MCP event above.
    commit = library.commit(stage, proposal, file_sha(library.root / 'state.json'), [name], ['public-task'])
    saved = current_skill(config, name)
    return {'persisted': True, 'source_actor': 'executor', 'stage': stage,
            'name': name, 'raw_sha256': saved['raw_sha256'], 'commit': commit}
