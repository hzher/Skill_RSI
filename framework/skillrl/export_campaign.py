"""Auditable full-suite export with human-readable traces and exact skill bodies."""
import argparse
from collections import Counter, defaultdict
import csv
import io
import json
from pathlib import Path
from .storage import atomic, file_sha, immutable, load
from .export_pastbench import completed_response


def read_if(path, fallback=None):
    return load(path) if path.exists() else fallback


def export_campaign(campaign, destination):
    campaign, destination = Path(campaign).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('Export directory exists; do not overwrite a release')
    summary_path = campaign / ('campaign-reconciled.json' if (campaign / 'campaign-reconciled.json').exists() else 'campaign-summary.json')
    final = load(summary_path)
    plan = load(campaign / 'campaign-plan.json')
    destination.mkdir(parents=True)
    summaries, all_rows, successful, skill_versions, holds, native_turns = [], [], [], {}, [], []
    costs = defaultdict(lambda: Counter())
    for family in plan['families']:
        family_root = campaign / 'families' / family
        summary = read_if(family_root / 'family-summary.json', {})
        family_rows = summary.get('episodes', read_if(family_root / 'family-progress.json', []))
        family_stats = {'family': family, 'category': family.split('/')[0], 'attempted': len(family_rows),
                        'execution_complete': 0, 'pipeline_complete': 0, 'passed': 0, 'failed': 0, 'unknown': 0,
                        'skill_reads': 0, 'memory_writes': 0, 'session_searches': 0, 'scores': []}
        for episode in family_rows:
            root = Path(episode['report']).parent
            task = root / 'episodes' / episode['task_id']
            execution = read_if(task / 'execution.json')
            outcome = episode.get('objective')
            report = read_if(root / 'report.json', {})
            score = outcome['score'] if outcome else None
            family_stats['passed' if score == 1 else 'failed' if score == 0 else 'unknown'] += 1
            family_stats['execution_complete'] += execution is not None
            family_stats['pipeline_complete'] += bool(episode['complete'])
            if outcome and outcome['score'] is not None:
                family_stats['scores'].append(outcome['details'].get('task_score'))
            wire = []
            for request_path in sorted((task / 'actor/wire').glob('*/request.json')):
                request = load(request_path)
                response_path = request_path.parent / 'response.raw'
                response, failure = None, read_if(request_path.parent / 'failure.json')
                try:
                    response = completed_response(response_path)
                except (OSError, ValueError) as exc:
                    failure = failure or {'kind': type(exc).__name__, 'reason': str(exc)}
                if response and response.get('model') is not None and response['model'] != request['model']:
                    raise ValueError('Unexpected reported model in native wire; reconcile before publishing data')
                wire.append({'request': request, 'response': response, 'failure': failure,
                    'request_sha256': file_sha(request_path),
                    'response_sha256': file_sha(response_path) if response_path.exists() else None,
                    'source': str(request_path.parent)})
            original_exposures = read_if(task / 'exposure.json', [])
            exposures = original_exposures
            posthoc = campaign / 'posthoc-code-mode-exposure-v1/tasks' / family / episode['task_id']
            posthoc_intent = read_if(posthoc / 'intent.json')
            posthoc_review = None
            if posthoc_intent and (posthoc / 'exposure.json').exists():
                expected_sha = file_sha(task / 'execution.json') if execution is not None else None
                if posthoc_intent['source_execution_sha256'] != expected_sha:
                    raise ValueError('Posthoc proof is not bound to this original execution')
                exposures = load(posthoc / 'exposure.json')
                posthoc_review = read_if(posthoc / 'review.json')
            eligible = bool(execution and execution['complete'] and episode['complete'] and score == 1
                            and wire and all(w['response'] is not None and w['failure'] is None for w in wire))
            hold = read_if(task / 'hold.json', read_if(root / 'driver-error.json'))
            row = {'schema': 'skillrl-pastbench-campaign-native-v1', 'family': family, **episode,
                   'source_commit': plan['upstream_commit'], 'execution': execution, 'native_wire': wire,
                   'exposure': exposures, 'original_runtime_exposure': original_exposures,
                   'exposure_analysis_ref': str(posthoc) if posthoc_intent else None,
                   'posthoc_skill_review': posthoc_review,
                   'hold': hold, 'successful_trajectory': eligible,
                   'curation': read_if(task / 'curator/result.json'), 'curator_ref': str(task / 'curator'),
                   'training_logprobs_available': False, 'policy_gradient_eligible': False}
            all_rows.append(row)
            if eligible:
                successful.append(row)
                for index, turn in enumerate(wire):
                    # A separate model-only view avoids accidentally feeding private
                    # grading metadata or post-execution state into an SFT prompt.
                    native_turns.append({'schema': 'skillrl-successful-native-turn-v1', 'family': family,
                        'task_id': episode['task_id'], 'turn_index': index, 'task_passed': True,
                        'request': turn['request'], 'response': turn['response'],
                        'request_sha256': turn['request_sha256'], 'response_sha256': turn['response_sha256'],
                        'source': turn['source'], 'policy_gradient_eligible': False})
            if not episode['complete'] or score is None:
                holds.append({'family': family, 'episode': episode['episode'], 'task_id': episode['task_id'],
                              'hold': hold, 'objective': outcome, 'source': str(root)})
            for role, cost in report.get('cost', {}).items():
                for key in ('request_attempts', 'complete_responses', 'known_input_lower_bound',
                            'known_output_lower_bound', 'missing_usage', 'known_unsent', 'invalid_complete_responses',
                            'physical_request_attempts', 'transport_recovered_errors', 'prior_transport_attempts_with_unknown_cost'):
                    costs[role][key] += cost.get(key, 0)
            events = execution['events'] if execution else []
            family_stats['skill_reads'] += sum(e.get('name') == 'Skill' and not e.get('error') for e in events)
            family_stats['memory_writes'] += sum(e.get('name') == 'memory' and not e.get('error')
                and e['arguments'].get('action') in ('add', 'replace', 'remove') for e in events)
            family_stats['session_searches'] += sum(e.get('name') == 'session_search' and not e.get('error') for e in events)
            state = read_if(root / 'library/state.json', {'active': {}, 'processed': {}})
            versions = list(state['active'].values())
            for commit in state.get('processed', {}).values():
                versions.extend(commit.get('applied_ops', []))
                versions.extend(commit.get('active', []))
            for version in versions:
                sha = version.get('raw_sha256')
                if not sha or not (root / 'library/raw_versions' / (sha + '.md')).exists():
                    continue
                body = (root / 'library/raw_versions' / (sha + '.md')).read_text()
                generated = str(version.get('updated_task', version.get('created_task', ''))).startswith('curate-')
                entry = skill_versions.setdefault(sha, {'raw_sha256': sha, 'name': version['name'], 'content': body,
                    'generated_candidate': generated, 'instances': [], 'successful_consumers': [], 'causal_effect_verified': False})
                entry['generated_candidate'] |= generated
                instance = {'family': family, 'task_id': episode['task_id'], 'skill_id': version.get('skill_id'),
                            'created_task': version.get('created_task'), 'updated_task': version.get('updated_task')}
                if instance not in entry['instances']:
                    entry['instances'].append(instance)
            for proof in exposures:
                sha = proof['raw_sha256']
                if eligible and proof['body_proven'] and any(p['subsequent_actions'] for p in proof['proof']) and sha in skill_versions:
                    skill_versions[sha]['successful_consumers'].append({'family': family, 'task_id': episode['task_id'], 'proof': proof['proof']})
            trace = '# ' + episode['task_id'] + '\n\nFamily: ' + family + '\n\n'
            trace += 'Original pass: ' + str(score) + '; pipeline complete: ' + str(episode['complete']) + '\n\n'
            trace += 'Original result directory: `' + str(root) + '`\n\n'
            if execution:
                prompt = read_if(task / 'actor/prompt.json', {}).get('prompt', '')
                trace += '## Actual initial prompt\n\n' + prompt + '\n\n## Actual tool events\n\n'
                for i, event in enumerate(events):
                    if event['type'] == 'tool':
                        trace += '### ' + str(i) + ': ' + event['name'] + '\n\n```json\n' + json.dumps(event, ensure_ascii=False, indent=2) + '\n```\n\n'
                trace += '## Final answer\n\n' + execution['final'] + '\n\n'
            else:
                trace += 'No sealed complete execution; see original attempt files.\n\n'
            trace += '## Original objective receipt\n\n```json\n' + json.dumps(outcome, ensure_ascii=False, indent=2) + '\n```\n'
            trace_path = destination / 'traces' / family / (episode['task_id'] + '.md')
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            immutable(trace_path, trace.encode())
        family_stats['mean_task_score'] = sum(s for s in family_stats.pop('scores') if s is not None) / max(1, family_stats['passed'] + family_stats['failed'])
        summaries.append(family_stats)
    for sha, entry in skill_versions.items():
        p = destination / 'skills' / (entry['name'] + '--' + sha[:12] + '.md')
        p.parent.mkdir(exist_ok=True)
        immutable(p, entry['content'].encode())
        entry['view_file'] = str(p.relative_to(destination))
        entry['status'] = 'observed_successful_consumer' if entry['successful_consumers'] else 'candidate' if entry['generated_candidate'] else 'provided_seed'
    for name, rows in [('all-trajectories.jsonl', all_rows), ('successful-trajectories.jsonl', successful),
                       ('skills.jsonl', list(skill_versions.values())), ('unresolved.jsonl', holds),
                       ('successful-native-turns.jsonl', native_turns)]:
        immutable(destination / name, ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows).encode())
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(summaries[0]))
    writer.writeheader()
    writer.writerows(summaries)
    immutable(destination / 'families.csv', stream.getvalue().encode())
    totals = {key: sum(s[key] for s in summaries) for key in ('attempted', 'execution_complete', 'pipeline_complete',
        'passed', 'failed', 'unknown', 'skill_reads', 'memory_writes', 'session_searches')}
    result = {'schema': 'skillrl-pastbench-full-export-v1', 'source_commit': plan['upstream_commit'],
        'families': summaries, 'totals': totals, 'successful_training_trajectories': len(successful),
        'successful_native_turns': len(native_turns),
        'skill_versions': len(skill_versions), 'generated_skill_versions': sum(s['generated_candidate'] for s in skill_versions.values()),
        'provided_seed_only_versions': sum(not s['generated_candidate'] for s in skill_versions.values()),
        'skill_versions_with_successful_consumer': sum(bool(s['successful_consumers']) for s in skill_versions.values()),
        'cost': dict(costs), 'policy_gradient_eligible': False, 'weights_updated': False,
        'campaign_ref': str(campaign), 'campaign_plan_sha256': file_sha(campaign / 'campaign-plan.json'),
        'campaign_summary_ref': str(summary_path), 'campaign_summary_sha256': file_sha(summary_path),
        'exporter_sha256': file_sha(Path(__file__)), 'objective_contract': 'Original grade_episode and original hard-pass predicates; Codex/framework substrate adapter',
        'files': {p.name: file_sha(p) for p in destination.iterdir() if p.is_file()}}
    text = '# PAST-Bench full-suite results\n\n' + json.dumps(totals, ensure_ascii=False, indent=2) + '\n\n'
    text += '| Family | Attempts | Pass | Fail | Unknown | Mean task_score | Skill reads |\n|---|---:|---:|---:|---:|---:|---:|\n'
    for s in summaries:
        text += f"| {s['family']} | {s['attempted']} | {s['passed']} | {s['failed']} | {s['unknown']} | {s['mean_task_score']:.4f} | {s['skill_reads']} |\n"
    text += '\n## Individual traces\n\n'
    for row in all_rows:
        relative = 'traces/' + row['family'] + '/' + row['task_id'] + '.md'
        text += '- [' + row['task_id'] + '](' + relative + ') — ' + str(row['objective']['score'] if row['objective'] else None) + '\n'
    text += '\n## Skills (exact original model/seed bodies)\n\n'
    for entry in skill_versions.values():
        text += '- [' + entry['name'] + ' ' + entry['raw_sha256'][:12] + '](' + entry['view_file'] + ') — ' + entry['status'] + '\n'
    text += '\nNo causal skill-effect claim, independent downstream evaluation or weight update. Unknown scores remain null.\n'
    immutable(destination / 'RESULTS.md', text.encode())
    result['files'] = {str(p.relative_to(destination)): file_sha(p) for p in destination.rglob('*') if p.is_file()}
    atomic(destination / 'manifest.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = export_campaign(args.campaign, args.output)
    print(json.dumps({k: v for k, v in result.items() if k not in ('families', 'files')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
