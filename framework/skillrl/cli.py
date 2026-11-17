"""Configuration-only entry point for installed and copied standalone packages."""
from pathlib import Path
import argparse
import importlib
import json
from .agents import ToolExecutor
from .native import ClaudeCodeExecutor
from .benchmarks import ManifestBenchmark, WorkBuddyBenchmark, JSONExactEvaluator, CommandEvaluator
from .curation import Curator
from .models import HTTPModel
from .pipeline import Pipeline
from .review import Reviewer
from .storage import writer
from .vendor.library import DeltaLibrary


def plugin(spec):
    module, name = spec['factory'].split(':', 1)
    return getattr(importlib.import_module(module), name)(**spec.get('options', {}))

def build(config, base):
    base = Path(base)
    def location(value):
        p = Path(value)
        return p if p.is_absolute() else (base / p).resolve()
    root = location(config['run_dir'])
    bench = config['benchmark']
    evaluation = bench.get('evaluator', {'kind': 'none'})
    if evaluation['kind'] == 'json_exact':
        evaluator = JSONExactEvaluator(location(evaluation['expected_dir']), evaluation.get('output', 'output/result.json'))
    elif evaluation['kind'] == 'command':
        evaluator = CommandEvaluator(evaluation['argv'], timeout=evaluation.get('timeout', 900))
    elif evaluation['kind'] == 'plugin':
        evaluator = plugin(evaluation)
    elif evaluation['kind'] == 'none':
        evaluator = None
    else:
        raise ValueError('Unknown objective evaluator')
    if bench['kind'] == 'plugin':
        benchmark = plugin(bench)
    else:
        cls = WorkBuddyBenchmark if bench['kind'] == 'workbuddy' else ManifestBenchmark
        if bench['kind'] not in ('workbuddy', 'manifest'):
            raise ValueError('Unknown benchmark adapter')
        options = {'image': bench.get('image'), 'evaluator': evaluator, 'network': bench.get('network', 'none'),
                   'user': bench.get('user'), 'security_profile': bench.get('security_profile', 'hardened')}
        if bench['kind'] == 'workbuddy':
            options['images'] = bench.get('images')
        benchmark = cls(location(bench['manifest']), **options)
    models = {role: HTTPModel(**options) for role, options in config['models'].items()}
    execution = config.get('executor', {})
    backend = execution.get('backend', 'openai-tools')
    options = {key: value for key, value in execution.items() if key != 'backend'}
    if backend not in ('claude-code', 'openai-tools'):
        raise ValueError('Unknown executor backend')
    executor = (ClaudeCodeExecutor if backend == 'claude-code' else ToolExecutor)(models['executor'], **options)
    return Pipeline(root, benchmark, executor, Curator(models['curator'], **config.get('curator', {})),
                    Reviewer(models['judge'], **config.get('reviewer', {})), sequence=config.get('sequence', 'skillrl'))

def main():
    parser = argparse.ArgumentParser(description='Portable execution, skill management and independent review')
    parser.add_argument('config', type=Path)
    parser.add_argument('--check', action='store_true', help='Validate configuration and public files without model calls')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    pipeline = build(config, args.config.resolve().parent)
    if args.check:
        print(json.dumps({'tasks': len(pipeline.benchmark.tasks()), 'run_dir': str(pipeline.root), 'model_calls': 0}))
        return
    if config.get('seed_skills'):
        with writer(pipeline.root):
            library = DeltaLibrary(pipeline.root / 'library', pipeline.sequence)
            operations = []
            for name in config['seed_skills']:
                source = Path(name)
                if not source.is_absolute():
                    source = args.config.resolve().parent / source
                from .vendor.library_base import parse_skill
                content = source.read_text()
                fields, _ = parse_skill(content)
                from .vendor.library import quality
                quality(content)
                operations.append({'name': 'new_skill_insert', 'arguments': {'skill_name': fields['name'], 'content': content}})
            library.apply('manual-bootstrap', operations)
    report = pipeline.run()
    print(json.dumps({'report': str(pipeline.root / 'report.json'), 'complete': report['complete'],
                      'tasks': report['task_count'], 'active_skills': report['active_skills']}, ensure_ascii=False))

if __name__ == '__main__':
    main()
