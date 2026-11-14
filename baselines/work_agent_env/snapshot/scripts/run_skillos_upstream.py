"""Plan or collect pinned SkillOS 8x10 groups through the Toolathlon/API bridge.

Default is entirely CPU/offline. --execute uses already running role services;
it never starts model services or launches an optimizer.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integrations.benchmark_tasks import digest, load_task_set, strict_json
from integrations.skill_generator.skillos.upstream import Upstream, REVISION
from integrations.skill_generator.skillos.bridge import make_plan


def load_config(path):
    config = strict_json(Path(path).read_text())
    expected = {'schema_version': 'workagent-skillos-upstream-v1', 'source_commit': REVISION,
                'source_root': 'integrations/skill_generator/vendor/skillos_upstream',
                'collector_source': 'snapshots/2026-10-29/source204/rollout_backend/rollout_alfweb.py',
                'actor_source': 'snapshots/2026-10-29/source204/actor_backend/actor_update.py',
                'formal_groups': 32, 'rollouts_per_group': 8, 'positions_per_rollout': 10,
                'max_active_groups': 4, 'max_active_episodes': 32,
                'advantage': 'rollout_reward_minus_group_mean', 'normalize_std': False,
                'loss_aggregation': 'equal_complete_rollout_weight_mean_response_tokens_inside_rollout'}
    for key, value in expected.items():
        if config.get(key) != value or type(config.get(key)) is not type(value):
            raise ValueError('Configuration diverges from pinned SkillOS: ' + key)
    if config.get('optimizer_migration_verified') is not False:
        raise ValueError('Optimizer migration requires separate real acceptance')
    return config


async def execute(upstream, config, plan, tasks, output):
    if not plan['engineering']:
        raise ValueError('Formal collection requires reviewed 32-group lineage admission; not yet migrated')
    from integrations.executors import ExecutorOptions
    from integrations.orchestration.executor import DownstreamExecutor
    from integrations.skill_generator.roles import SkillOSRoles, load_roles
    from integrations.skill_generator.skillos.bridge import collect
    from scripts.run_skill_pipeline import load_credentials
    load_credentials(ROOT / config['executor_credentials_file'])
    roles = SkillOSRoles(load_roles(ROOT / config['roles_file']))
    try:
        ready = await roles.preflight()
        if not ready['ready']:
            raise ValueError('Already-running pinned role services are not ready')
        fixed = await DownstreamExecutor.create(project_root=ROOT, options=ExecutorOptions(**config['executor']),
            tasks=tasks['tasks'])
        return await collect(upstream=upstream, plan=plan, task_set=tasks, directory=output,
                             roles=roles, fixed_executor=fixed)
    finally:
        await roles.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/skillos_upstream.json')
    parser.add_argument('--task-set', type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--groups', type=Path, help='JSON array of explicit ordered groups')
    selection.add_argument('--plan-first-group', action='store_true', help='Diagnostic first 10 only; never training admission')
    parser.add_argument('--engineering', action='store_true', help='One complete 8x10 group; never a formal batch')
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    config, upstream = load_config(args.config), Upstream()
    tasks = load_task_set(args.task_set)
    if args.plan_first_group:
        if not args.engineering:
            raise ValueError('First-group diagnostic selection requires --engineering')
        groups = [{'group_id': 'diagnostic-first10', 'ordered_task_ids': [t['uid'] for t in tasks['tasks'][:10]],
                   'rollout_seeds': [42 + i for i in range(8)]}]
    else:
        groups = strict_json(args.groups.read_text())
    plan = make_plan(tasks, groups=groups, run_id=args.run_id, engineering=args.engineering)
    result = {'status': 'planned_not_executed', 'plan': plan, 'configuration_sha256': digest(config),
        'upstream_files_verified': len(upstream.manifest['files']), 'planned_task_environment_attempts': len(groups) * 80,
        'actual_task_environment_attempts': 0,
        'actual_parameter_updates': 0, 'training_admitted': False, 'optimizer_migration_verified': False}
    if args.execute:
        batch, summary = asyncio.run(execute(upstream, config, plan, tasks, ROOT / '.runtime/skillos-upstream' / args.run_id))
        result.update(status=batch['status'], summary=summary, actual_task_environment_attempts=len(groups) * 80)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
