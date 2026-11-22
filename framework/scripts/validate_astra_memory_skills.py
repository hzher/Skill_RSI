"""Source-family single-skill screening followed by a fresh full-suite ablation.

Requires a frozen extraction catalog and per-family release configurations.
Uses the unchanged original task grader through the existing Codex harness.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import hashlib
import json
import os
import random
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from skillrl.storage import atomic, canonical, file_sha, load


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(root):
    return {str(p.relative_to(root)): file_sha(p) for p in sorted(root.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc' and '.git' not in p.parts}


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)


def measurements(root, family, expected_skills):
    summary_file = root / 'family-summary.json'
    partial = root / 'family-progress.json'
    rows = load(summary_file)['episodes'] if summary_file.exists() else load(partial) if partial.exists() else []
    results = []
    for row in rows:
        base = Path(row['report']).parent
        task = base / 'episodes' / row['task_id']
        objective = row.get('objective') or {}
        continuous = (objective.get('details') or {}).get('task_score')
        known = objective.get('score') is not None
        exposure = False
        binding = False
        execution_file = task / 'execution.json'
        execution = load(execution_file) if execution_file.exists() else {}
        if execution:
            provided = execution.get('static_context_skills', [])
            if provided != expected_skills:
                raise ValueError('Unexpected static skill exposure: ' + str(task))
            wire = sorted((task / 'actor/wire').glob('*/request.json'))
            completed_wire = [p for p in wire if (p.parent / 'result.json').exists()]
            # Each skill must occur literally in actual completed input, not just catalog/settings.
            exposure = all(any(s['content'] in text for p in completed_wire for text in strings(load(p)))
                           for s in expected_skills)
            if not exposure:
                raise ValueError('Missing literal skill text in completed native wire: ' + str(task))
            original = base / 'evaluation' / row['task_id'] / 'objective/original-grade.json'
            if known and original.exists():
                binding = objective['score'] == int(load(original)['passed'])
                if not binding:
                    raise ValueError('Objective differs from original grader')
        results.append({'family': family, 'task_id': row['task_id'], 'episode': row['episode'],
                        'branch': row['branch'], 'execution_complete': bool(execution),
                        'passed': objective.get('score') == 1 if known else None,
                        'reward': continuous if known else None, 'binary_reward': objective.get('score'),
                        'static_exposure_verified': exposure, 'original_score_bound': binding,
                        'usage': execution.get('usage'), 'wall_seconds': execution.get('wall_seconds'),
                        'scores': (objective.get('details') or {}).get('scores'),
                        'infra_blocked': (objective.get('details') or {}).get('infra_blocked'),
                        'tool_calls': sum(e.get('type') == 'tool' for e in execution.get('events', [])),
                        'report': row['report']})
    return results


def paired(a, b, planned):
    left = {(r['family'], r['task_id']): r for r in a}
    right = {(r['family'], r['task_id']): r for r in b}
    valid = [(left[k], right[k]) for k in sorted(left.keys() & right.keys())
             if left[k]['reward'] is not None and right[k]['reward'] is not None
             and left[k]['execution_complete'] and right[k]['execution_complete']]
    n = len(valid)
    bins = {'both_pass': 0, 'baseline_only_pass': 0, 'skills_only_pass': 0, 'both_fail': 0}
    for x, y in valid:
        bins['both_pass' if x['passed'] and y['passed'] else 'baseline_only_pass' if x['passed'] else
             'skills_only_pass' if y['passed'] else 'both_fail'] += 1
    return {'planned': planned, 'paired_scored': n, 'complete': n == planned,
            'baseline_scored': sum(r['reward'] is not None for r in a),
            'skills_scored': sum(r['reward'] is not None for r in b),
            'baseline_passed': sum(r['passed'] is True for r in a),
            'skills_passed': sum(r['passed'] is True for r in b),
            'mean_reward_baseline': sum(x['reward'] for x, y in valid) / n if n else None,
            'mean_reward_skills': sum(y['reward'] for x, y in valid) / n if n else None,
            'mean_reward_delta': sum(y['reward'] - x['reward'] for x, y in valid) / n if n else None,
            'pass_delta': sum(int(y['passed']) - int(x['passed']) for x, y in valid) / n if n else None,
            'paired_pass_table': bins}


def arm_summary(rows, planned):
    known = [r for r in rows if r['reward'] is not None]
    executed = [r for r in rows if r['execution_complete']]
    def score_mean(name):
        values = [(r.get('scores') or {}).get(name) for r in known]
        values = [v for v in values if isinstance(v, (int, float))]
        return sum(values) / len(values) if values else None
    return {'planned': planned, 'executed': len(executed), 'scored': len(known),
            'passed': sum(r['passed'] is True for r in known),
            'mean_reward_scored': sum(r['reward'] for r in known) / len(known) if known else None,
            'pass_rate_all_positions': sum(r['passed'] is True for r in known) / planned,
            'pass_rate_scored': sum(r['passed'] is True for r in known) / len(known) if known else None,
            'input_tokens_known': sum((r.get('usage') or {}).get('input_tokens', 0) for r in executed),
            'output_tokens_known': sum((r.get('usage') or {}).get('output_tokens', 0) for r in executed),
            'mean_tool_calls': sum(r['tool_calls'] for r in executed) / len(executed) if executed else None,
            'mean_wall_seconds': sum(r.get('wall_seconds') or 0 for r in executed) / len(executed) if executed else None,
            'mean_safety': score_mean('safety'), 'mean_robustness': score_mean('robustness'),
            'money': None, 'currency_price_unavailable': True}


def cluster_interval(a, b):
    left = {(r['family'], r['task_id']): r for r in a}
    groups = {}
    for r in b:
        x = left.get((r['family'], r['task_id']))
        if x and x['reward'] is not None and r['reward'] is not None:
            groups.setdefault(r['family'], []).append(r['reward'] - x['reward'])
    if len(groups) < 2:
        return None
    families = sorted(groups)
    generator = random.Random(20261119)
    estimates = []
    for _ in range(5000):
        values = [v for f in generator.choices(families, k=len(families)) for v in groups[f]]
        estimates.append(sum(values) / len(values))
    estimates.sort()
    return {'lower': estimates[125], 'upper': estimates[4874], 'method': 'family-cluster bootstrap percentile 95%',
            'replicates': 5000, 'seed': 20261119, 'families': len(groups),
            'limitation': 'Exploratory interval; same-suite skill selection and model routing uncertainty remain.'}


class Experiment:
    def __init__(self, args):
        self.args, self.root = args, args.output.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.reports = self.root / 'reports'
        self.reports.mkdir(exist_ok=True)
        self.catalog = load(args.extraction / 'catalog.json')
        self.sources = load(args.extraction / 'skill-sources.json')
        self.families = load(args.release / 'families.json')
        self.family_map = {f['family']: f for f in self.families}
        self.buckets = {(r['family'], r['task_id']): r['bucket'] for r in
                        (json.loads(line) for line in (args.release / 'trajectories-index.jsonl').read_text().splitlines())}
        self.env = os.environ.copy()
        self.env['PYTHONPATH'] = str(self.root / 'framework')
        self.plan = self.freeze()

    def freeze(self):
        target = self.root / 'plan.json'
        if target.exists():
            plan = load(target)
            if plan['catalog_sha256'] != file_sha(self.args.extraction / 'catalog.json'):
                raise ValueError('Frozen catalog changed')
            if identity(self.root / 'framework') != plan['framework_files_sha256']:
                raise ValueError('Frozen framework changed')
            if identity(self.root / 'benchmark') != plan['benchmark_files_sha256']:
                raise ValueError('Frozen benchmark changed')
            return plan
        framework = Path(__file__).resolve().parents[1] / 'skillrl'
        shutil.copytree(framework, self.root / 'framework/skillrl', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        # Preserve Git identity and all working-tree task/scorer modifications.
        subprocess.run(['git', 'clone', '--shared', '--quiet', str(self.args.benchmark), str(self.root / 'benchmark')], check=True)
        for name in ('src', 'mock_services', 'self-evolve-tasks-v2'):
            shutil.rmtree(self.root / 'benchmark' / name)
            shutil.copytree(self.args.benchmark / name, self.root / 'benchmark' / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        configs = {}
        pinned = load(self.args.release / 'provenance/rerun-plan.json')['source_files_sha256']
        for relative, expected in pinned.items():
            if file_sha(self.root / 'benchmark' / relative) != expected:
                raise ValueError('Benchmark differs from collection freeze: ' + relative)
        for item in self.families:
            family = item['family']
            config = load(self.args.release / 'families' / family.replace('/', '__') / 'run-config.json')
            assert 'static_context_skills' not in config.get('codex', {})
            assert config.get('family_transport_attempts', 1) == 1
            configs[family] = config
        plan = {'started_at': now(), 'protocol': 'astra-full-extract-single-skill-source-screen-full26-v2',
                'catalog_sha256': file_sha(self.args.extraction / 'catalog.json'),
                'sources_sha256': file_sha(self.args.extraction / 'skill-sources.json'),
                'framework_files_sha256': identity(self.root / 'framework'),
                'benchmark_files_sha256': identity(self.root / 'benchmark'), 'configs': configs,
                'families': self.families, 'workers': self.args.workers,
                'screen_rule': 'Select only if all source-family task positions are paired/scored and mean continuous task_score delta > 0.',
                'screen_baseline': 'One fresh baseline per source family shared across individual skills; never use old collection scores.',
                'confirm_baseline': 'All 26 families freshly rerun after selection; no screening baseline reused.',
                'static_prompt_intervention': True, 'task_or_grader_changes': False,
                'same_suite_selection_bias': True, 'unseen_task_generalization_claim': False,
                'weights_updated': False}
        plan['collection_benchmark_sha256_verified'] = len(pinned)
        atomic(target, plan)
        return plan

    def job(self, phase, arm, family, skills):
        root = self.root / 'runs' / phase / arm / family
        receipt = self.reports / 'jobs' / phase / arm / (family.replace('/', '__') + '.json')
        if receipt.exists():
            return load(receipt)
        config = json.loads(json.dumps(self.plan['configs'][family]))
        if skills:
            config['codex']['static_context_skills'] = skills
        config_file = self.root / 'configs' / phase / arm / (family.replace('/', '__') + '.json')
        atomic(config_file, config)
        log = self.root / 'worker-logs' / phase / arm / (family.replace('/', '__') + '.log')
        log.parent.mkdir(parents=True, exist_ok=True)
        state_file = self.reports / 'job-states' / phase / arm / (family.replace('/', '__') + '.json')
        prior = load(state_file) if state_file.exists() else None
        # Rebuild analysis after a finished worker without replaying any actions.
        # A process without an exit receipt always needs explicit reconciliation.
        if root.exists() and (not prior or 'exit_code' not in prior):
            raise RuntimeError('Partial family requires explicit reconciliation: ' + str(root))
        cmd = [str(self.args.logger), '--artifact', str(root / 'family-summary.json'), '--', sys.executable,
               '-u', '-m', 'skillrl.pastbench_campaign', '--worker', '--source', str(self.root / 'benchmark'),
               '--config', str(config_file), '--output', str(root), '--family', family]
        if prior and 'exit_code' in prior:
            if prior['config_sha256'] != file_sha(config_file):
                raise ValueError('Cannot reuse finished worker with changed configuration')
            exit_code = prior['exit_code']
        else:
            with log.open('x') as stream:
                proc = subprocess.Popen(cmd, env=self.env, stdout=stream, stderr=subprocess.STDOUT)
                state = {'pid': proc.pid, 'started_at': now(), 'config_sha256': file_sha(config_file), 'argv': cmd}
                atomic(state_file, state)
                exit_code = proc.wait()
                atomic(state_file, {**state, 'exit_code': exit_code, 'finished_at': now()})
        rows = measurements(root, family, skills)
        for row in rows:
            row['bucket'] = self.buckets[(family, row['task_id'])]
        from skillrl.reporting import costs
        value = {'phase': phase, 'arm': arm, 'family': family, 'exit_code': exit_code,
                 'finished_at': now(), 'skills': [s['id'] for s in skills], 'rows': rows,
                 'expected_tasks': self.family_map[family]['planned'], 'run': str(root)}
        value['cost_by_task'] = {r['task_id']: costs(Path(r['report']).parent) for r in rows}
        atomic(receipt, value)
        print(json.dumps({'event': 'family_finished', 'phase': phase, 'arm': arm, 'family': family,
                          'scored': sum(r['reward'] is not None for r in rows),
                          'passed': sum(r['passed'] is True for r in rows)}), flush=True)
        return value

    def jobs(self, specs):
        results = []
        with ThreadPoolExecutor(max_workers=self.args.workers) as pool:
            pending = {pool.submit(self.job, *spec): spec for spec in specs}
            while pending:
                done, _ = wait(pending, timeout=20, return_when=FIRST_COMPLETED)
                for future in done:
                    spec = pending.pop(future)
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        atomic(self.reports / 'job-errors' / spec[0] / spec[1] / (spec[2].replace('/', '__') + '.json'),
                               {'kind': type(exc).__name__, 'reason': str(exc), 'spec': spec[:3]})
                        raise
                atomic(self.reports / 'progress.json', {'observed_at': now(), 'stage': specs[0][0] if specs else None,
                    'jobs_total': len(specs), 'jobs_complete': len(results),
                    'scored_positions_complete': sum(sum(r['reward'] is not None for r in v['rows']) for v in results)})
        return results

    def run(self):
        source_families = sorted({f for s in self.sources for f in s['families']})
        baselines = [('screen', 'baseline', family, []) for family in source_families]
        treatments = [('screen', 'skill--' + s['skill_id'], family,
                       [next(c for c in self.catalog if c['id'] == s['skill_id'])])
                      for s in self.sources for family in s['families']]
        # Deterministic interleaving, never reward-dependent ordering.
        specs = []
        for family in source_families:
            specs += [s for s in baselines if s[2] == family] + [s for s in treatments if s[2] == family]
        atomic(self.reports / 'screen-plan.json', {'jobs': [s[:3] for s in specs],
            'skills': len(self.catalog), 'source_families': len(source_families),
            'task_positions': sum(self.family_map[s[2]]['planned'] for s in specs),
            'selection_rule': self.plan['screen_rule'], 'created_at': now()})
        print(json.dumps({'event': 'part2_started', 'jobs': len(specs),
                          'task_positions': sum(self.family_map[s[2]]['planned'] for s in specs)}), flush=True)
        screen = self.jobs(specs)
        metrics = []
        for source in self.sources:
            skill_id, families = source['skill_id'], set(source['families'])
            a = [r for job in screen if job['arm'] == 'baseline' and job['family'] in families for r in job['rows']]
            b = [r for job in screen if job['arm'] == 'skill--' + skill_id for r in job['rows']]
            result = paired(a, b, sum(self.family_map[f]['planned'] for f in families))
            result.update(skill_id=skill_id, name=source['name'], families=sorted(families))
            result['selected'] = result['complete'] and result['mean_reward_delta'] > 1e-12
            metrics.append(result)
        selected = [c for c in self.catalog if any(m['selected'] and m['skill_id'] == c['id'] for m in metrics)]
        atomic(self.reports / 'screen-results.json', metrics)
        atomic(self.reports / 'selected-skills.json', selected)
        atomic(self.reports / 'part2-completion.json', {'finished_at': now(), 'skills_tested': len(metrics),
            'skills_selected': len(selected), 'all_comparisons_complete': all(m['complete'] for m in metrics),
            'screening_selection_bias': True})
        text = ['# Astra skill 初测结果', '', f'初测 {len(metrics)} 项，入选 {len(selected)} 项。', '',
                '按每项技能全部来源任务组完整配对的连续reward正提升筛选；基线为本轮新运行。未评分不作零分。', '',
                '|Skill|来源组数|配对/计划|基线reward|Skill reward|差值|入选|', '|---|---:|---:|---:|---:|---:|---|']
        for m in metrics:
            text.append(f"|{m['skill_id']}|{len(m['families'])}|{m['paired_scored']}/{m['planned']}|"
                        f"{m['mean_reward_baseline']}|{m['mean_reward_skills']}|{m['mean_reward_delta']}|{m['selected']}|")
        text += ['', '同题集筛选观察，不代表独立泛化验证。Part 3另跑全量两臂。']
        (self.reports / 'PART2.md').write_text('\n'.join(text) + '\n')
        print(json.dumps({'event': 'part2_complete', 'skills_tested': len(metrics), 'selected': len(selected)}), flush=True)
        if not selected:
            atomic(self.reports / 'completion.json', {'finished_at': now(), 'status': 'no_improving_skills',
                'part3': 'No selected skill pack; no nonempty treatment to evaluate.', 'reward_improvement_demonstrated': False})
            return
        confirm_specs = []
        for i, family in enumerate(sorted(self.family_map)):
            arms = [('baseline', []), ('selected-skills', selected)]
            if i % 2:
                arms.reverse()
            confirm_specs += [('confirm', arm, family, pack) for arm, pack in arms]
        atomic(self.reports / 'confirm-plan.json', {'created_at': now(), 'jobs': [s[:3] for s in confirm_specs],
            'selected_ids': [s['id'] for s in selected], 'task_positions': 408,
            'selected_pack_sha256': hashlib.sha256(canonical(selected)).hexdigest()})
        confirm = self.jobs(confirm_specs)
        a = [r for job in confirm if job['arm'] == 'baseline' for r in job['rows']]
        b = [r for job in confirm if job['arm'] == 'selected-skills' for r in job['rows']]
        overall = paired(a, b, 204)
        per_family = {family: paired([r for r in a if r['family'] == family],
                                    [r for r in b if r['family'] == family], item['planned'])
                      for family, item in self.family_map.items()}
        buckets = sorted(set(self.buckets.values()))
        per_bucket = {bucket: paired([r for r in a if r['bucket'] == bucket],
                                    [r for r in b if r['bucket'] == bucket],
                                    sum(v == bucket for v in self.buckets.values())) for bucket in buckets}
        atomic(self.reports / 'confirm-results.json', {'overall': overall, 'families': per_family,
            'buckets': per_bucket, 'baseline_rows': a, 'skills_rows': b,
            'baseline_summary': arm_summary(a, 204), 'skills_summary': arm_summary(b, 204),
            'reward_delta_interval': cluster_interval(a, b),
            'same_suite_not_unseen_generalization': True})
        unchanged = (identity(self.root / 'framework') == self.plan['framework_files_sha256'] and
                     identity(self.root / 'benchmark') == self.plan['benchmark_files_sha256'])
        atomic(self.reports / 'completion.json', {'finished_at': now(), 'status': 'finished',
            'full26_paired_complete': overall['complete'], 'sources_unchanged': unchanged,
            'overall': overall, 'selected_skills': len(selected),
            'reward_improvement_observed': overall['mean_reward_delta'] > 0 if overall['mean_reward_delta'] is not None else None,
            'causal_generalization_certified': False, 'weights_updated': False})
        print(json.dumps({'event': 'part3_complete', **overall, 'sources_unchanged': unchanged}), flush=True)
        text = ['# 入选 Astra skills：26组新运行对照', '',
                f"入选 {len(selected)} 项；有效配对 {overall['paired_scored']}/204。", '',
                f"基线平均reward={overall['mean_reward_baseline']}；技能包={overall['mean_reward_skills']}；差值={overall['mean_reward_delta']}。", '',
                f"基线通过 {overall['baseline_passed']}/204；技能包通过 {overall['skills_passed']}/204。", '',
                '|任务组|配对/计划|基线reward|技能包reward|差值|', '|---|---:|---:|---:|---:|']
        for family, m in per_family.items():
            text.append(f"|{family}|{m['paired_scored']}/{m['planned']}|{m['mean_reward_baseline']}|{m['mean_reward_skills']}|{m['mean_reward_delta']}|")
        text += ['', '完整阶段分层、成本、约束指标及按任务族bootstrap区间见confirm-results.json和jobs/回执。',
                 '同题集筛选与单次新运行，不能据此认证未见任务泛化。未评分与API/运行故障单独保留，未按低分重跑。']
        (self.reports / 'PART3.md').write_text('\n'.join(text) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extraction', type=Path, required=True)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--benchmark', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--logger', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    assert load(args.extraction / 'completion.json')['complete']
    visibility = load(args.extraction / 'visibility-audit.json')
    assert visibility['validated'] and not visibility['extra_available_skill_bodies_sent']
    Experiment(args).run()


if __name__ == '__main__':
    main()
