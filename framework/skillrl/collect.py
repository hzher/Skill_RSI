"""Collect native CL-Bench rollouts with Claude Code or Codex."""

import argparse
from collections import Counter
from contextlib import chdir
import json
from pathlib import Path
import shutil

from .clbench import execute
from .continual import execute_chain
from .storage import atomic, identifier, writer
from .vendor.common import canonical, immutable


def select_tasks(plan, mode):
    if mode not in ('grouped', 'shuffled', 'independent', 'stateful'):
        raise ValueError('Unknown collection mode')
    tasks = plan['tasks'] if mode in ('independent', 'stateful') else plan['selected_tasks']
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
    if mode == 'stateful':
        from .codex_stateful import validate_stateful_plan
        validate_stateful_plan(tasks)
        return tasks
    if mode == 'independent':
        return tasks
    order = plan[mode + '_order']
    if len(order) != len(tasks) or set(order) != seen_ids:
        raise ValueError('Persistent order must contain every selected task exactly once')
    by_id = {task['id']: task for task in tasks}
    return [by_id[task_id] for task_id in order]


def validate_backend(config, mode):
    backend = config.get('backend', 'claude')
    if backend == 'codex':
        if mode not in ('independent', 'stateful'):
            raise ValueError('Codex supports independent or stateful collection')
        from .codex_chatgpt import validate_config
        validate_config(config)
        configured = config.get('execution_mode')
        if configured and configured != mode and not (configured == 'fresh' and mode == 'independent'):
            raise ValueError('Configured execution mode conflicts with the selected collection mode')
    elif backend != 'claude' or mode == 'stateful':
        raise ValueError('Unsupported native backend or collection mode')


def run_collection(config, plan, mode, output):
    validate_backend(config, mode)
    tasks = select_tasks(plan, mode)
    root = Path(output).resolve()
    repo = Path(config['repository']).resolve()
    if config.get('backend') == 'codex':
        config = {**config, 'repository': str(repo)}
    with writer(root), chdir(repo):
        if mode == 'stateful':
            from .codex_stateful import execute_sequence
            return execute_sequence(config, tasks, root)
        if mode != 'independent':
            return execute_chain(config, tasks, root)

        # This manifest permits reuse only with the same ordered inputs and configuration.
        immutable(root / 'collection.json', canonical({'mode': mode, 'tasks': tasks, 'configuration': config}))
        records = []
        stop_reason = None
        for index, task in enumerate(tasks):
            case = root / 'cases' / f'{index + 1:03d}-{task["id"]}'
            if (config.get('min_free_bytes') and not (case / 'result.json').exists()
                    and shutil.disk_usage(root).free < config['min_free_bytes']):
                stop_reason = 'insufficient_free_disk_for_evidence'
                break
            record = {'task': task, 'output': str(case.relative_to(root)), 'complete': False}
            try:
                kwargs = {'task_id': task['id']} if config.get('backend') == 'codex' else {}
                result = execute(config, task['family'], task['canonical_index'], case, **kwargs)
                record['complete'] = result['complete']
                record['initialization_terminal'] = result.get('initialization_terminal', False)
                if config.get('stop_on_account_capacity') and result.get('auth_mode') == 'chatgpt':
                    errors = json.dumps((result.get('native') or {}).get('errors', [])).lower()
                    if any(marker in errors for marker in ('usage limit', 'rate limit', 'rate_limit',
                                                           'quota', 'too many requests', 'insufficient credits')):
                        stop_reason = 'account_capacity_unavailable'
            except Exception as error:
                # Each case has its own intent. An unresolved case is recorded, never replayed here.
                record['error'] = {'type': type(error).__name__, 'message': str(error)}
            records.append(record)
            atomic(root / 'PROGRESS.json', {'visited': len(records), 'planned': len(tasks),
                   'completed_tasks': sum(row['complete'] for row in records), 'last_task': task['id']})
            if stop_reason:
                break
        result = {'mode': mode, 'tasks': len(tasks), 'completed_tasks': sum(row['complete'] for row in records),
                  'complete': len(records) == len(tasks) and all(row['complete'] for row in records), 'records': records}
        if stop_reason:
            result.update(stop_reason=stop_reason, unstarted_tasks=tasks[len(records):])
        atomic(root / 'collection-result.json', result)
        return result


def main():
    parser = argparse.ArgumentParser(description='Collect native CL-Bench rollouts with Claude Code or Codex')
    parser.add_argument('--mode', choices=['grouped', 'shuffled', 'independent', 'stateful'], required=True,
                        help='Independent resets each task. Stateful continues one Codex thread and native environment per family.')
    parser.add_argument('--config', type=Path, required=True, help='JSON launch settings; select backend=codex for the account CLI')
    parser.add_argument('--plan', type=Path, required=True, help='Codex reads the ordered tasks array (id, family, canonical_index)')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check', action='store_true', help='Check the manifest only, without Docker, credentials or model calls')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    plan = json.loads(args.plan.read_text())
    if args.check:
        validate_backend(config, args.mode)
        tasks = select_tasks(plan, args.mode)
        families = Counter(t['family'] for t in tasks)
        sessions = {'independent': len(tasks), 'stateful': len(families)}.get(args.mode, 1)
        print(json.dumps({'mode': args.mode, 'tasks': len(tasks), 'families': dict(families),
                          'planned_task_sessions': sessions,
                          'model_calls': 0, 'check_scope': 'input manifest only'}))
        return
    if args.output is None:
        parser.error('--output is required for execution')
    result = run_collection(config, plan, args.mode, args.output)
    print(json.dumps({'complete': result['complete'], 'tasks': result['tasks'], 'output': str(args.output)}))
    raise SystemExit(0 if result['complete'] else 1)


if __name__ == '__main__':
    main()
