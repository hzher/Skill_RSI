"""H20 GPU deployment configuration and offline commands. Never imports torch."""
from copy import deepcopy
import json
from pathlib import Path
import re

from integrations.benchmark_tasks.contracts import digest, strict_json
from integrations.skill_generator.skillos.upstream import REVISION

ROOT = Path(__file__).resolve().parents[1]


def load_config(path, *, root=ROOT):
    config = strict_json(Path(path).read_text())
    fields = {'schema_version', 'source_commit', 'model_path', 'model_revision', 'roles_file',
        'executor_config', 'storage_root', 'control_root', 'runs_root', 'services_root', 'gpu',
        'training', 'checkpoint', 'vllm_version', 'verified_manifest', 'training_registry'}
    if set(config) != fields or config['schema_version'] != 'workagent-skillos-h20-v1' or config['source_commit'] != REVISION:
        raise ValueError('Invalid pinned SkillOS H20 deployment')
    if config['model_revision'] != 'b968826d9c46dd6066d109eabc6255188de91218':
        raise ValueError('H20 actor must use the pinned Qwen3-8B base')
    gpu = config['gpu']
    if set(gpu) != {'device_type', 'physical_ids', 'world_size', 'minimum_memory_mib', 'idle_memory_limit_mib'}:
        raise ValueError('Invalid H20 GPU binding')
    ids = gpu['physical_ids']
    if (gpu['device_type'] != 'H20' or gpu['world_size'] != 4 or type(gpu['world_size']) is not int
            or not isinstance(ids, list) or len(ids) != len(set(ids)) or len(ids) != 4
            or any(type(i) is not int or i < 0 for i in ids)
            or type(gpu['minimum_memory_mib']) is not int or gpu['minimum_memory_mib'] < 90000
            or gpu['idle_memory_limit_mib'] != 512):
        raise ValueError('Four distinct full H20 devices are required; no CPU/single-rank fallback')
    train = config['training']
    fixed = {'groups_per_update': 32, 'rollouts_per_group': 8, 'positions_per_rollout': 10,
             'learning_rate': 1e-6, 'clip_ratio': .2, 'gradient_clip': 1.0, 'normalize_std': False, 'seed': 42}
    if set(train) != set(fixed) | {'phase_timeout_seconds'} or any(
            train[k] != v or type(train[k]) is not type(v) for k, v in fixed.items()):
        raise ValueError('Training configuration differs from the pinned four-rank SkillOS objective')
    if type(train['phase_timeout_seconds']) is not int or train['phase_timeout_seconds'] < 60:
        raise ValueError('Invalid distributed timeout')
    if config['checkpoint'] != {'active_capacity_bytes': 300000000000, 'free_reserve_bytes': 214748364800,
            'native_upper_bound_bytes': 110000000000, 'export_upper_bound_bytes': 40000000000, 'max_native_states': 2}:
        raise ValueError('Preserve original bounded native checkpoint accounting')
    if config['vllm_version'] != '0.11.0':
        raise ValueError('The relocated worker adapter currently targets the pinned local vLLM 0.11.0')
    for key in ('model_path', 'roles_file', 'executor_config', 'storage_root', 'control_root',
                'runs_root', 'services_root', 'verified_manifest', 'training_registry'):
        value = config[key]
        if not isinstance(value, str) or not value.strip() or '..' in Path(value).parts:
            raise ValueError('Invalid deployment path: ' + key)
        resolved = (Path(root) / value).absolute()
        if resolved.resolve() != resolved:
            raise ValueError('Symlinked deployment path: ' + key)
        config[key] = str(resolved)
    roots = [Path(config[k]) for k in ('storage_root', 'control_root', 'runs_root', 'services_root')]
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(roots) for b in roots[i+1:]):
        raise ValueError('Storage/control/runs/services roots must be separate')
    if any(Path(config['model_path']).is_relative_to(r) or r.is_relative_to(Path(config['model_path'])) for r in roots):
        raise ValueError('Base weights must be outside mutable runtime roots')
    return config


def run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise ValueError('Invalid native run ID')
    return value


def paths(config, name, step):
    run_id(name)
    if type(step) is not int or step < 0:
        raise ValueError('Invalid update index')
    work = Path(config['runs_root']) / name / f'update-{step:04d}'
    checkpoint = Path(config['storage_root']) / 'checkpoints' / f'{name}--step{step + 1}'
    return {'work': str(work), 'context': str(work / 'context.json'),
        'batch': str(work / 'collection/sealed_batch.json'), 'app': str(work / 'collection/actor_application_state.json'),
        'checkpoint': str(checkpoint), 'partial': str(checkpoint) + '.partial',
        'export': str(Path(config['storage_root']) / 'serving-policies' / checkpoint.name)}


def commands(config, name, step, *, project_root=ROOT):
    bound = paths(config, name, step)
    base = ['bash', str(Path(project_root) / 'scripts/generator_python.sh'), '-m', 'torch.distributed.run',
            '--standalone', '--nnodes=1', '--nproc-per-node=4',
            str(Path(project_root) / 'scripts/skillos_h20_worker.py'), '--context', bound['context']]
    common = {'CUBLAS_WORKSPACE_CONFIG': ':4096:8', 'TOKENIZERS_PARALLELISM': 'false',
              'PYTHONHASHSEED': '42'}
    devices = ','.join(map(str, config['gpu']['physical_ids']))
    return {stage: {'argv': base + ['--stage', stage],
            'environment': {**common, 'CUDA_VISIBLE_DEVICES': '' if stage == 'export' else devices},
            'world_size': 4, 'device': 'CPU gloo export of GPU-trained FSDP shards' if stage == 'export' else 'H20 NCCL FSDP',
            'executed': False} for stage in ('actor', 'validate', 'export')}


def offline_plan(config, name, step=0):
    return {'schema_version': 'workagent-skillos-h20-plan-v1',
        'status': 'planned_not_executed', 'config_sha256': digest(config), 'run_id': run_id(name), 'update_index': step,
        'paths': paths(config, name, step), 'commands': commands(config, name, step),
        'resource_plan': {'training': deepcopy(config['gpu']), 'collection_role_local_ranks': {
            'curator': [0], 'selfjudge': [1], 'contentjudge': [2, 3]},
            'phase_exclusion': 'Drain collection and stop owned role services before four-H20 actor/validation'},
        'requires': ['reviewed 32x8x10 sampled batch and exact task lineage',
                     'actual initial/installed Curator and frozen role fingerprints',
                     'complete pinned base weights and native full-state lineage',
                     'four idle full H20 cards and checkpoint storage headroom'],
        'gpu_probed': False, 'torch_imported': False, 'actual_parameter_updates': 0}
