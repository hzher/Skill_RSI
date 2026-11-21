"""Run each official family in its own process with bounded parallelism."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from .storage import atomic, file_sha, load


def tasks_complete(summary):
    """A finished task group stays finished regardless of reward/skill review."""
    return bool(summary and summary.get('attempts_finished') and summary.get('episodes') and
        all(row.get('execution_complete') and row.get('objective') and
            row['objective'].get('score') is not None for row in summary['episodes']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--family')
    parser.add_argument('--families', nargs='+')
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if args.worker:
        from .pastbench_full import run_full_family
        result = run_full_family(source, args.family, load(args.config), output)
        print(json.dumps({'event': 'family_done', 'family': args.family, 'passed': result['passed'],
                          'complete': result['complete'], 'known_scores': result['known_scores']}), flush=True)
        return
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / 'source'
    if snapshot.exists():
        raise ValueError('Campaign source snapshot exists; use explicit reconciliation rather than blind restart')
    snapshot.mkdir()
    shutil.copytree(Path(__file__).parent, snapshot / 'skillrl', ignore=shutil.ignore_patterns('__pycache__'))
    families = [str(p.parent.relative_to(source / 'self-evolve-tasks-v2'))
                for p in sorted((source / 'self-evolve-tasks-v2').glob('*/*/family.yaml'))]
    if args.family:
        families = [args.family]
    elif args.families:
        if not set(args.families) <= set(families):
            raise ValueError('Unknown family selection')
        families = args.families
    configuration = load(args.config)
    family_attempts = int(configuration.get('family_transport_attempts', 1))
    if not 1 <= family_attempts <= 10:
        raise ValueError('family_transport_attempts outside 1..10')
    atomic(output / 'campaign-plan.json', {'families': families, 'workers': args.workers,
        'config': configuration, 'upstream_commit': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
        'runtime_source_sha256': {str(p.relative_to(snapshot)): file_sha(p) for p in snapshot.rglob('*.py')}})

    def run_attempt(family, root):
        root.mkdir(parents=True)
        env = os.environ.copy()
        env['PYTHONPATH'] = str(snapshot)
        argv = [sys.executable, '-m', 'skillrl.pastbench_campaign', '--worker', '--family', family,
                '--source', str(source), '--config', str(args.config.resolve()), '--output', str(root)]
        with (root / 'worker.log').open('w') as stream:
            proc = subprocess.Popen(argv, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            atomic(root / 'worker.json', {'pid': proc.pid, 'argv': argv})
            code = proc.wait()
        summary = load(root / 'family-summary.json') if (root / 'family-summary.json').exists() else None
        result = {'family': family, 'exit_code': code, 'summary': str(root / 'family-summary.json'),
            'episodes': len(summary['episodes']) if summary else 0, 'passed': summary['passed'] if summary else 0,
            'known_scores': summary['known_scores'] if summary else 0, 'complete': summary['complete'] if summary else False}
        atomic(root / 'worker-result.json', result)
        return result

    def run(family):
        if family_attempts == 1:
            return run_attempt(family, output / 'families' / family)
        attempts = []
        for index in range(family_attempts):
            root = output / 'family-attempts' / family / f'{index:04d}'
            result = run_attempt(family, root)
            exhausted = [str(p) for p in root.rglob('transport-exhausted.json')
                         if load(p).get('transient')]
            attempts.append({**result, 'attempt': index, 'transient_exhaustions': exhausted})
            summary = load(root / 'family-summary.json') if (root / 'family-summary.json').exists() else None
            if tasks_complete(summary) or not exhausted:
                break
            if index + 1 < family_attempts:
                print(json.dumps({'event': 'family_transport_retry', 'family': family,
                    'failed_attempt': index, 'retained': str(root)}), flush=True)
                time.sleep(15)
        # This is an index, never a relocated/overwritten attempt. Reports retain
        # their actual absolute paths, including complete branch lineage.
        selected = output / 'families' / family
        selected.mkdir(parents=True, exist_ok=False)
        for name in ('family-summary.json', 'family-progress.json', 'upstream-manifest.yaml'):
            if (root / name).exists():
                shutil.copyfile(root / name, selected / name)
        result = {**result, 'attempts': attempts, 'selected_attempt': len(attempts) - 1,
                  'selection_rule': 'first fully executed/scored task group; no reward or skill-review selection',
                  'summary': str(selected / 'family-summary.json')}
        atomic(selected / 'worker-result.json', result)
        return result

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(run, family): family for family in families}
        for future in as_completed(pending):
            try:
                result = future.result()
            except Exception as exc:
                result = {'family': pending[future], 'error': type(exc).__name__ + ': ' + str(exc)}
            results.append(result)
            atomic(output / 'campaign-progress.json', results)
            print(json.dumps(result), flush=True)
    atomic(output / 'campaign-summary.json', {'families': results, 'families_requested': len(families),
        'episodes_attempted': sum(r.get('episodes', 0) for r in results),
        'known_scores': sum(r.get('known_scores', 0) for r in results),
        'passed': sum(r.get('passed', 0) for r in results),
        'complete': all(r.get('complete') for r in results)})


if __name__ == '__main__':
    main()
