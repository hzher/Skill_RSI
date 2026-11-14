"""Plan/check the real four-H20 SkillOS deployment, or explicitly execute a stage.

Without --execute, bootstrap/collect/prepare/update are plans only. ``compile``
compiles relocated source definitions but never imports torch or starts CUDA.
"""
import argparse
import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from training.skillos_h20_config import load_config, offline_plan, paths


def compile_deployment(config, name, step=0):
    before = set(sys.modules)
    from training.skillos_h20_runtime import Runtime
    from training.skillos_h20_sources import Sources
    from training.skillos_h20_services import Services
    runtime = Runtime.__new__(Runtime)
    runtime.config, runtime.name, runtime.step = config, name, step
    runtime.paths, runtime.model = paths(config, name, step), Path(config['model_path'])
    runtime.sources, runtime.modules = Sources(config), None
    runtime.build()
    # GPU worker imports are compiled, not executed, during this check.
    for relative in ('training/skillos_h20_worker_model.py', 'scripts/skillos_h20_worker.py', 'scripts/serve_skillos_content.py'):
        source = ROOT / relative
        compile(ast.parse(source.read_text()), str(source), 'exec')
    loaded = set(sys.modules) - before
    if any(name == 'torch' or name.startswith(('torch.', 'vllm.', 'transformers.')) for name in loaded):
        raise RuntimeError('Offline compilation unexpectedly imported a GPU/model runtime')
    services = Services(config, name, project_root=ROOT)
    role_config = services.roles(f'V{step:03d}', config['model_path'])
    return {'status': 'compiled_not_executed', 'source_adaptations': runtime.sources.audit,
            'service_commands': {name: services.command(name, role) for name, role in role_config['roles'].items()},
            'torch_imported_by_check': False, 'gpu_probed': False, 'gpu_jobs_started': 0,
            'actual_parameter_updates': 0, 'real_gpu_runtime_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/skillos_h20.json')
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--step', type=int, default=0)
    parser.add_argument('--action', choices=('plan', 'compile', 'status', 'bootstrap', 'collect', 'prepare',
                                           'actor', 'validate', 'export', 'install', 'update', 'retire'), default='plan')
    parser.add_argument('--task-set', type=Path)
    parser.add_argument('--admission', type=Path)
    parser.add_argument('--collection', type=Path)
    parser.add_argument('--context', type=Path)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.action == 'compile':
        result = compile_deployment(config, args.run_id, args.step)
    elif args.action == 'status':
        from training.skillos_h20_state import State
        result = State(paths(config, args.run_id, args.step)['work']).get()
    elif args.action == 'plan' or not args.execute:
        result = {**offline_plan(config, args.run_id, args.step), 'requested_action': args.action}
    else:
        from training.skillos_h20 import Controller
        from integrations.benchmark_tasks import load_task_set
        controller = Controller(config, args.run_id, project_root=ROOT)
        if args.action == 'bootstrap':
            if args.task_set is None:
                parser.error('--task-set is required for bootstrap')
            result = controller.bootstrap(load_task_set(args.task_set))
        elif args.action == 'collect':
            if args.admission is None:
                parser.error('--admission is required for reviewed formal collection')
            result = controller.collect(args.admission)
        elif args.action == 'prepare':
            if args.collection is None or args.admission is None:
                parser.error('--collection and --admission are required for prepare')
            result = controller.prepare(args.collection, args.admission)
        elif args.action == 'retire':
            result = controller.retire()
        else:
            if args.context is None:
                parser.error('--context is required for native lifecycle stages')
            if args.action == 'update':
                result = {}
                for stage in ('actor', 'validate', 'export', 'install'):
                    result[stage] = controller.run_stage(args.context, stage)
            else:
                result = controller.run_stage(args.context, args.action)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
