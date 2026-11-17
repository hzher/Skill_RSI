"""Three collection modes backed by the existing native Claude Code runners."""

import argparse
from collections import Counter
from contextlib import chdir
import json
from pathlib import Path

from .clbench import execute
from .continual import execute_chain
from .storage import atomic, identifier, writer
from .vendor.common import canonical, immutable


def select_tasks(plan, mode):
    if mode not in ('grouped', 'shuffled', 'independent'):
        raise ValueError('Unknown collection mode')
    tasks = plan['tasks'] if mode == 'independent' else plan['selected_tasks']
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('A collection needs a nonempty task list')
    seen_ids, seen_instances = set(), set()
    for task in tasks:
        task_id = identifier(task['id'])
        family = identifier(task['family'])
        index = task['canonical_index']
        if type(index) is not int or index < 0:
            raise ValueError('canonical_index must be a nonnegative integer')
        if task.get('schedule', 'default') != 'default':
            raise ValueError('The native binding currently supports the default schedule')
        if task_id in seen_ids or (family, index) in seen_instances:
            raise ValueError('Duplicate task ID or official instance')
        seen_ids.add(task_id)
        seen_instances.add((family, index))
    if mode == 'independent':
        return tasks
    order = plan[mode + '_order']
    if len(order) != len(tasks) or set(order) != seen_ids:
        raise ValueError('Persistent order must contain every selected task exactly once')
    by_id = {task['id']: task for task in tasks}
    return [by_id[task_id] for task_id in order]


def run_collection(config, plan, mode, output):
    tasks = select_tasks(plan, mode)
    root = Path(output).resolve()
    with writer(root), chdir(config['repository']):
        if mode != 'independent':
            return execute_chain(config, tasks, root)

        # This manifest permits reuse only with the same ordered inputs and configuration.
        immutable(root / 'collection.json', canonical({'mode': mode, 'tasks': tasks, 'configuration': config}))
        records = []
        for index, task in enumerate(tasks):
            case = root / 'cases' / f'{index + 1:03d}-{task["id"]}'
            record = {'task': task, 'output': str(case.relative_to(root)), 'complete': False}
            try:
                result = execute(config, task['family'], task['canonical_index'], case)
                record['complete'] = result['complete']
                record['initialization_terminal'] = result.get('initialization_terminal', False)
            except Exception as error:
                # Each case has its own intent. An unresolved case is recorded, never replayed here.
                record['error'] = {'type': type(error).__name__, 'message': str(error)}
            records.append(record)
            atomic(root / 'PROGRESS.json', {'visited': len(records), 'planned': len(tasks),
                   'completed_tasks': sum(row['complete'] for row in records), 'last_task': task['id']})
        result = {'mode': mode, 'tasks': len(tasks), 'completed_tasks': sum(row['complete'] for row in records),
                  'complete': all(row['complete'] for row in records), 'records': records}
        atomic(root / 'collection-result.json', result)
        return result


def main():
    parser = argparse.ArgumentParser(description='Native Claude Code rollout collection')
    parser.add_argument('--mode', choices=['grouped', 'shuffled', 'independent'], required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check', action='store_true', help='Check the manifest only, without Docker, credentials or model calls')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    plan = json.loads(args.plan.read_text())
    tasks = select_tasks(plan, args.mode)
    if args.check:
        print(json.dumps({'mode': args.mode, 'tasks': len(tasks), 'families': dict(Counter(t['family'] for t in tasks)),
                          'planned_task_sessions': len(tasks) if args.mode == 'independent' else 1,
                          'model_calls': 0, 'check_scope': 'input manifest only'}))
        return
    if args.output is None:
        parser.error('--output is required for execution')
    result = run_collection(config, plan, args.mode, args.output)
    print(json.dumps({'complete': result['complete'], 'tasks': result['tasks'], 'output': str(args.output)}))
    raise SystemExit(0 if result['complete'] else 1)


if __name__ == '__main__':
    main()
