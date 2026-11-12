"""Export native, source-bound PAST-Bench data; never relabel unknown as failure."""
import argparse
import json
from pathlib import Path
from .storage import atomic, load, file_sha, immutable


def completed_response(path):
    terminal = None
    for line in path.read_text(errors='replace').splitlines():
        if line.startswith('data: '):
            try:
                event = json.loads(line[6:])
            except ValueError:
                continue
            if event.get('type') == 'response.completed':
                terminal = event['response']
    if terminal is None:
        raise ValueError('No complete native response: ' + str(path))
    return terminal


def export(family_root, destination):
    family_root, destination = Path(family_root).resolve(), Path(destination).resolve()
    summary = load(family_root / 'family-summary.json')
    if not summary['complete']:
        raise ValueError('Require a completed family before release export')
    destination.mkdir(parents=True, exist_ok=False)
    plan = load(family_root / 'family-plan.json')
    all_rows, accepted, skills = [], [], {}
    for episode in summary['episodes']:
        root = Path(episode['report']).parent
        task_id = episode['task_id']
        task = root / 'episodes' / task_id
        execution = load(task / 'execution.json')
        wire = []
        for request_path in sorted((task / 'actor/wire').glob('*/request.json')):
            response_path = request_path.parent / 'response.raw'
            response = completed_response(response_path)
            request = load(request_path)
            if response.get('model') is not None and response['model'] != request['model']:
                raise ValueError('Reported model differs from requested model; reconcile before release')
            wire.append({'request': request, 'response': response,
                         'request_sha256': file_sha(request_path), 'response_sha256': file_sha(response_path),
                         'reported_model_present': response.get('model') is not None})
        if not wire:
            raise ValueError('Native wire evidence absent')
        objective = episode['objective']
        eligible = bool(episode['complete'] and objective and objective['score'] == 1
                        and execution['complete'] and any(e['type'] == 'tool' and not e['error']
                            and e['name'] not in ('Skill', 'memory') for e in execution['events']))
        row = {'schema': 'skillrl-pastbench-native-v1', 'family': summary['family'],
            'episode': episode['episode'], 'task_id': task_id, 'branch': episode['branch'],
            'source_commit': plan['upstream_commit'], 'runtime_source_sha256': load(root / 'plan.json')['runtime_source_sha256'],
            'executor': load(task / 'actor/command.json'), 'execution': execution,
            'objective': objective, 'exposure': load(task / 'exposure.json'),
            'native_wire': wire, 'successful_trajectory': eligible,
            'training_logprobs_available': False, 'policy_gradient_eligible': False,
            'curator_ref': str(task / 'curator'), 'source_run': str(root)}
        all_rows.append(row)
        if eligible:
            accepted.append(row)
        state = load(root / 'library/state.json')
        for name, version in state['active'].items():
            body = root / 'library/raw_versions' / (version['raw_sha256'] + '.md')
            entry = skills.setdefault(version['raw_sha256'], {
                **version, 'content': body.read_text(), 'observed_in': [], 'successful_consumers': [],
                'status': 'candidate', 'causal_effect_verified': False})
            entry['observed_in'].append(task_id)
        for proof in row['exposure']:
            if eligible and proof['body_proven']:
                version = proof['raw_sha256']
                if version not in skills:
                    entry = next(s for s in execution['skills_at_start'] if s['raw_sha256'] == version)
                    skills[version] = {**entry, 'observed_in': [], 'successful_consumers': [],
                                      'status': 'candidate', 'causal_effect_verified': False}
                skills[version]['successful_consumers'].append({'task_id': task_id, 'proof': proof['proof']})
                skills[version]['status'] = 'observed_successful_consumer'
    for name, values in [('all-trajectories.jsonl', all_rows), ('successful-trajectories.jsonl', accepted),
                         ('skills.jsonl', list(skills.values()))]:
        immutable(destination / name, ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in values).encode())
    result = {'family': summary['family'], 'episodes': len(all_rows), 'successful_trajectories': len(accepted),
              'skill_versions': len(skills), 'skill_versions_with_successful_consumer': sum(
                  bool(s['successful_consumers']) for s in skills.values()),
              'scope': 'single-family development data; not independent downstream evaluation',
              'policy_gradient_eligible': False, 'weights_updated': False,
              'exporter_sha256': file_sha(Path(__file__)),
              'files': {p.name: file_sha(p) for p in destination.iterdir() if p.is_file()}}
    atomic(destination / 'manifest.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family-run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.family_run, args.output), ensure_ascii=False))


if __name__ == '__main__':
    main()
