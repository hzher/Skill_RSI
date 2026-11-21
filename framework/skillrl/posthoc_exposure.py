"""Append code-mode exposure proofs and reviews without changing original runs."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
import json
import time
from .agents import codex_exposures
from .benchmarks import Task
from .models import HTTPModel
from .review import Reviewer
from .storage import atomic, begin, file_sha, load, seal


def analyze(campaign, family, episode, output, model):
    root = Path(episode['report']).parent
    task_dir = root / 'episodes' / episode['task_id']
    target = output / 'tasks' / family / episode['task_id']
    target.mkdir(parents=True, exist_ok=True)
    execution_path = task_dir / 'execution.json'
    intent = {'source_run': str(root), 'source_task_id': episode['task_id'],
        'source_execution_sha256': file_sha(execution_path) if execution_path.exists() else None,
        'original_exposure_sha256': file_sha(task_dir / 'exposure.json') if (task_dir / 'exposure.json').exists() else None,
        'checker_sha256': file_sha(Path(__file__).parent / 'agents.py'),
        'reviewer_sha256': file_sha(Path(__file__).parent / 'review.py'),
        'original_objective': episode['objective'], 'original_pipeline_complete': episode['complete']}
    cached = begin(target, intent)
    if cached is not None:
        return cached
    if not execution_path.exists():
        atomic(target / 'exposure.json', [])
        return seal(target, {'family': family, 'task_id': episode['task_id'], 'status': 'no_sealed_execution', 'proven': 0})
    execution = load(execution_path)
    snapshot = load(task_dir / 'snapshot.json')
    source_plan = load(root / 'plan.json')
    action_tools = source_plan['reviewer']['action_tools']
    exposure = codex_exposures(execution, snapshot, task_dir / 'actor', action_tools)
    atomic(target / 'exposure.json', exposure)
    original = load(task_dir / 'exposure.json')
    public = source_plan['tasks'][0]
    task = Task(public['id'], public['instruction'], root / 'public-artifacts')
    outcome = episode['objective'] or {'score': None, 'details': {'status': 'original_objective_absent'}}
    review = Reviewer(model, action_tools=action_tools).run(task, execution, snapshot, exposure, outcome, target / 'review')
    atomic(target / 'review.json', review)
    result = {'family': family, 'task_id': episode['task_id'], 'status': 'posthoc_review_recorded',
        'original_proven': sum(e['body_proven'] for e in original), 'proven': sum(e['body_proven'] for e in exposure),
        'used': sum(e.get('use_state') == 'used' for e in review['skills']),
        'invalid_reviews': sum(e['status'] == 'invalid_or_unresolved_review' for e in review['skills']),
        'causal_effect_verified': False, 'original_task_reward_unchanged': True}
    return seal(target, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    campaign, output = args.campaign.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    plan = load(campaign / 'campaign-plan.json')
    atomic(output / 'plan.json', {'source_campaign_plan_sha256': file_sha(campaign / 'campaign-plan.json'),
        'checker_sha256': file_sha(Path(__file__).parent / 'agents.py'),
        'reviewer_sha256': file_sha(Path(__file__).parent / 'review.py'),
        'workers': args.workers, 'scope': 'offline exact-input proof extraction and observational review; original runs untouched'})
    model = HTTPModel(**plan['config']['models']['judge'])
    seen, results, pending = set(), [], {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while True:
            for family in plan['families']:
                progress = campaign / 'families' / family / 'family-progress.json'
                if not progress.exists():
                    continue
                for episode in load(progress):
                    identity = (family, episode['task_id'])
                    if identity not in seen:
                        seen.add(identity)
                        pending[pool.submit(analyze, campaign, family, episode, output, model)] = identity
            done, _ = wait(pending, timeout=5, return_when=FIRST_COMPLETED) if pending else (set(), set())
            for future in done:
                identity = pending.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = {'family': identity[0], 'task_id': identity[1], 'status': 'unresolved_posthoc',
                              'error': type(exc).__name__ + ': ' + str(exc)[:1000]}
                results.append(result)
                atomic(output / 'progress.json', results)
                if result.get('proven') or result['status'] == 'unresolved_posthoc':
                    print(json.dumps(result), flush=True)
            finished = len(list((campaign / 'families').glob('*/*/worker-result.json'))) == len(plan['families'])
            if finished and not pending:
                break
            if not pending:
                time.sleep(10)
    atomic(output / 'summary.json', {'tasks': results, 'tasks_checked': len(results),
        'proven_exposures': sum(r.get('proven', 0) for r in results),
        'observed_used': sum(r.get('used', 0) for r in results),
        'invalid_reviews': sum(r.get('invalid_reviews', 0) for r in results),
        'unresolved_tasks': sum(r['status'] == 'unresolved_posthoc' for r in results),
        'original_runs_unchanged': True, 'causal_effect_verified': False})


if __name__ == '__main__':
    main()
