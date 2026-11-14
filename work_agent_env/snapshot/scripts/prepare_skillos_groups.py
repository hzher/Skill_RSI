"""Build review material for explicit SkillOS task groups; never grant admission."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integrations.benchmark_tasks import digest, load_task_set, strict_json
from integrations.skill_generator.skillos.bridge import make_plan
from training.seed_contract import prompt_digest
from training.skillos_h20_config import load_config
from training.skillos_h20_state import atomic, file_ref, read


def proposal(config, *, task_set_file, groups_file, run_id):
    tasks = load_task_set(task_set_file)
    groups = strict_json(Path(groups_file).read_text())
    plan = make_plan(tasks, groups=groups, run_id=run_id, engineering=False)
    selected = {uid for group in plan['groups'] for uid in group['source_record']['ordered_task_ids']}
    manifest = read(config['verified_manifest'])
    reserved = set(manifest['task_ids'])
    provenance, exact_conflicts = {}, []
    for task in tasks['tasks']:
        if task['uid'] not in selected:
            continue
        if task['task_id'] in reserved:
            exact_conflicts.append(task['uid'])
        provenance[task['uid']] = {'source': 'toolathlon_gym', 'split': 'train',
            'task_id': task['task_id'], 'task_family': None,
            'source_commit': task['source']['revision'].split('+', 1)[0],
            'seed_sha256': digest(task),
            'prompt_sha256': prompt_digest(task['prompt'], {'input': [{'role': 'user', 'content': task['prompt']}]}),
            'ancestor_tasks': None, 'ancestor_families': None}
    return {'schema_version': 'workagent-skillos-group-review-proposal-v1', 'training_admitted': False,
        'reviewed_registry_written': False, 'evaluation_bodies_read': False,
        'exact_reserved_task_conflicts': sorted(exact_conflicts),
        'requires_review': ['task family/aliases/ancestors', 'task order/curriculum rationale',
                            'evaluator prerequisites and train/development/final-test separation'],
        'candidate_admission': {'schema_version': 'workagent-skillos-group-admission-v1', 'review_id': None,
            'task_set': file_ref(task_set_file), 'groups': plan['groups'], 'provenance': provenance,
            'evaluation_manifest': file_ref(config['verified_manifest']),
            'training_registry': file_ref(config['training_registry'])}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/skillos_h20.json')
    parser.add_argument('--task-set', type=Path, required=True)
    parser.add_argument('--groups', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    result = proposal(load_config(args.config), task_set_file=args.task_set.absolute(),
                      groups_file=args.groups.absolute(), run_id=args.run_id)
    atomic(args.output.absolute(), result, new=True)
    print(json.dumps({'output': str(args.output.absolute()), 'groups': len(result['candidate_admission']['groups']),
        'exact_reserved_task_conflicts': result['exact_reserved_task_conflicts'], 'training_admitted': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
