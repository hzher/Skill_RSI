"""Extract general skills with Astra from public PAST-Bench execution windows.

Never sends grader details. All model requests, responses, input ranges and
source associations are retained separately from downstream skill text.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from skillrl.models import HTTPModel, ModelError
from skillrl.storage import atomic, canonical, file_sha, load

FIELDS = ('id', 'name', 'mechanism', 'triggers_and_preconditions', 'procedure',
          'verification', 'failure_recovery', 'do_not_use_when')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate(value, allowed):
    if not isinstance(value, dict) or not isinstance(value.get('skills'), list):
        raise ValueError('Expected object with skills array')
    notes = value.get('audit_notes', [])
    ids = set()
    for skill in value['skills']:
        if set(skill) != set(FIELDS) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', skill['id']):
            raise ValueError('Skill fields or id invalid')
        if skill['id'] in ids:
            raise ValueError('Duplicate skill id')
        ids.add(skill['id'])
        for field in FIELDS[1:]:
            expected = str if field in ('name', 'mechanism') else list
            if not isinstance(skill[field], expected) or not skill[field]:
                raise ValueError('Empty or invalid skill field ' + field)
            if expected is list and any(not isinstance(x, str) or not x.strip() for x in skill[field]):
                raise ValueError('Expected nonempty string steps')
        linked = [n for n in notes if n.get('skill_id') == skill['id']]
        refs = {s for n in linked for s in n.get('source_ids', [])}
        if not refs or not refs <= allowed:
            raise ValueError('Each skill needs exact valid source_ids: ' + skill['id'])
    for item in value.get('readback_requests', []):
        if item.get('source_id') not in allowed:
            raise ValueError('Unknown readback source id')
    return value


def markdown(skill):
    description = skill['name'] + '；' + '；'.join(skill['triggers_and_preconditions'])
    lines = ['---', 'name: ' + skill['id'],
             'description: ' + json.dumps(description, ensure_ascii=False), '---', '',
             '# ' + skill['name'], '', skill['mechanism']]
    for field, title in [('triggers_and_preconditions', '触发与前提'), ('procedure', '操作步骤'),
                         ('verification', '核验'), ('failure_recovery', '失败恢复'),
                         ('do_not_use_when', '不适用情形')]:
        lines += ['', '## ' + title, ''] + [f'{i}. {s}' for i, s in enumerate(skill[field], 1)]
    return '\n'.join(lines) + '\n'


def prepare(source, root, prompt_file, chunk_chars):
    rows = [json.loads(line) for line in (source / 'trajectories-index.jsonl').read_text().splitlines()]
    lookup = {(r['family'], r['task_id']): r for r in rows}
    sources, windows, files = {}, [], {}
    for archive in sorted((source / 'families').glob('*/all-trajectories.jsonl.gz')):
        files[str(archive.relative_to(source))] = file_sha(archive)
        units = []
        with gzip.open(archive, 'rt') as stream:
            for line in stream:
                row = json.loads(line)
                family, task = row['family'], row['task_id']
                index = lookup[(family, task)]
                sid = family + '::' + task
                execution = row.get('execution')
                trace = source / index['trace']
                text = trace.read_text()
                start = '## Actual initial prompt\n\n'
                end = '\n\n## Actual tool events'
                if not execution:
                    # The release explicitly has one unsealed/unscored attempt.
                    # Preserve its coverage gap without fabricating model input.
                    execution = {'events': [], 'final': ''}
                    parts = [('coverage_gap', 'No sealed complete execution or actual initial prompt in release. '
                              'Unscored; do not infer a strategy failure or extract a positive skill from this absence.')]
                    prompt = None
                elif start not in text or end not in text:
                    raise ValueError('Missing actual initial prompt: ' + sid)
                else:
                    prompt = text.split(start, 1)[1].split(end, 1)[0]
                    parts = [('actual_initial_prompt', prompt)]
                # The initial prompt already contains actual injected memory
                # and the skill catalog. Full available skill bodies are NOT
                # initial model input; only real Skill tool returns may expose
                # them. Never pass library_at_start or skills_at_start here.
                parts += [('public_permission_flags', execution.get('persistence_flags', {}))]
                parts += [('event:' + str(i), event) for i, event in enumerate(execution['events'])]
                parts += [('final_answer', execution['final'])]
                sources[sid] = {'family': family, 'task_id': task, 'trace': index['trace'],
                    'trace_sha256': file_sha(trace), 'archive': str(archive.relative_to(source)),
                    'passed_label': 'passed' if index['score'] == 1 else 'failed' if index['score'] == 0 else 'unscored',
                    'continuous_reward': index.get('reward'),
                    'events': len(execution['events']), 'units': []}
                sources[sid]['actual_initial_prompt_available'] = prompt is not None
                for label, body in parts:
                    raw = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                    for offset in range(0, max(1, len(raw)), chunk_chars):
                        unit = {'source_id': sid, 'field': label, 'char_start': offset,
                                'char_end': min(offset + chunk_chars, len(raw)), 'total_chars': len(raw),
                                'body': raw[offset:offset + chunk_chars]}
                        unit['sha256'] = digest(unit)
                        units.append(unit)
                        sources[sid]['units'].append({k: v for k, v in unit.items() if k != 'body'})
        # Bound each window by serialized character count; never truncate units.
        batches, batch, size = [], [], 0
        for unit in units:
            length = len(json.dumps(unit, ensure_ascii=False))
            if batch and size + length > chunk_chars * 2:
                batches.append(batch)
                batch, size = [], 0
            batch.append(unit)
            size += length
        if batch:
            batches.append(batch)
        for i, batch in enumerate(batches):
            name = family.replace('/', '__') + f'--{i:03d}'
            payload = {'mode': 'WINDOW_EXTRACT', 'family': family,
                       'source_index': {sid: {k: v for k, v in sources[sid].items() if k != 'units'}
                                        for sid in sorted({u['source_id'] for u in batch})},
                       'public_windows': batch}
            atomic(root / 'inputs' / (name + '.json'), payload)
            windows.append({'id': name, 'family': family, 'sha256': digest(payload),
                            'source_ids': sorted(payload['source_index']),
                            'unit_sha256': [u['sha256'] for u in batch]})
    assert len(sources) == len(rows) and sources
    plan = {'source': str(source), 'source_files_sha256': files, 'sources': sources, 'windows': windows,
            'prompt_sha256': file_sha(prompt_file), 'teacher': 'gpt-6-astra',
            'protocol': 'full-extraction-single-skill-screen-combined-full-v2',
            'private_fields_sent': False, 'skills_count_limit': None}
    plan['visibility_contract'] = 'actual-initial-prompt-and-tool-events-only-v3'
    atomic(root / 'input-index.json', plan)
    return plan


class Extractor:
    def __init__(self, root, prompt, plan, timeout):
        self.root, self.prompt, self.plan = root, prompt, plan
        self.model = HTTPModel('https://cn.affordablerouter.com/v1', 'gpt-6-astra',
                               'ASTRA_API_KEY', timeout=timeout, max_tokens=65536,
                               transport={'max_attempts': 4, 'max_elapsed': timeout,
                                          'attempt_timeout': min(timeout, 360)})

    def ask(self, name, payload, allowed):
        final = self.root / 'outputs' / (name + '.json')
        if final.exists():
            return validate(load(final), allowed)
        messages = [{'role': 'system', 'content': self.prompt},
                    {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
        for attempt in range(4):
            stage = self.root / 'api' / name / f'{attempt:02d}'
            # A text-only teacher call has no task/tool side effects. Retain an
            # ended failed attempt and use a separate ordinal on explicit resume.
            if (stage / 'failure.json').exists() and not (stage / 'result.json').exists():
                prior = load(stage / 'failure.json')
                if prior.get('status') in (400, 401, 403, 404):
                    raise ValueError('Terminal teacher HTTP error; reconcile route/schema')
                if (stage / 'response.raw').exists():
                    body = load(stage / 'response.raw')
                    if body.get('choices', [{}])[0].get('finish_reason') == 'length':
                        self.model.max_tokens *= 2
                continue
            try:
                result = self.model.call(messages, [], stage)
            except ModelError:
                if (stage / 'response.raw').exists():
                    body = load(stage / 'response.raw')
                    if body.get('choices', [{}])[0].get('finish_reason') == 'length':
                        self.model.max_tokens *= 2
                        continue
                raise
            if result.get('reported_model') != 'gpt-6-astra':
                raise ValueError('Teacher model identity missing or unexpected')
            content = result['message'].get('content', '')
            try:
                value = validate(json.loads(content), allowed)
                requests = value.get('readback_requests', [])
                if requests:
                    wanted = {r['source_id'] for r in requests}
                    readback = []
                    for w in self.plan['windows']:
                        if wanted & set(w['source_ids']):
                            inp = load(self.root / 'inputs' / (w['id'] + '.json'))
                            readback += [u for u in inp['public_windows'] if u['source_id'] in wanted]
                    messages += [{'role': 'assistant', 'content': content}, {'role': 'user', 'content':
                        json.dumps({'requested_public_readback': readback,
                                    'instruction': 'Use requested sources and return complete JSON; clear resolved readback_requests.'}, ensure_ascii=False)}]
                    continue
            except (ValueError, TypeError, KeyError) as exc:
                messages += [{'role': 'assistant', 'content': content}, {'role': 'user', 'content':
                    'Repair the complete JSON only; preserve quality and provenance. Validation error: ' + str(exc)}]
                continue
            atomic(final, value)
            atomic(self.root / 'coverage' / (name + '.json'), {'response_complete': True,
                'input_sha256': digest(payload), 'source_ids': sorted(allowed),
                'usage': result.get('usage'), 'reported_model': result['reported_model']})
            print(json.dumps({'event': 'teacher_completed', 'batch': name, 'skills': len(value['skills'])}), flush=True)
            return value
        raise ValueError('Teacher output unresolved after retained attempts: ' + name)

    def run(self, workers):
        results = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.ask, w['id'], load(self.root / 'inputs' / (w['id'] + '.json')),
                                   set(w['source_ids'])): w for w in self.plan['windows']}
            for future in as_completed(futures):
                window = futures[future]
                try:
                    results[window['id']] = future.result()
                except BaseException:
                    for other in futures:
                        other.cancel()
                    raise
                atomic(self.root / 'progress.json', {'stage': 'extracting', 'windows_total': len(futures),
                    'windows_complete': len(results), 'candidate_count': sum(len(v['skills']) for v in results.values())})
        # Consolidate per family first; then global merge. All writing is Astra's.
        families = {}
        for w in self.plan['windows']:
            families.setdefault(w['family'], []).append(results[w['id']])
        merged = []
        for family, values in families.items():
            allowed = {sid for sid, v in self.plan['sources'].items() if v['family'] == family}
            merged.append(self.ask('family-merge--' + family.replace('/', '__'),
                                   {'mode': 'GLOBAL_MERGE', 'candidates': values}, allowed))
        allowed = set(self.plan['sources'])
        global_result = self.ask('global-merge', {'mode': 'GLOBAL_MERGE', 'candidates': merged}, allowed)
        final = self.ask('generality-review', {'mode': 'GENERALITY_REVIEW', 'candidates': global_result}, allowed)
        catalog, source_map = [], []
        for skill in final['skills']:
            refs = sorted({sid for n in final['audit_notes'] if n['skill_id'] == skill['id']
                           for sid in n['source_ids']})
            content = markdown(skill)
            path = self.root / 'skills' / skill['id'] / 'SKILL.md'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            catalog.append({'id': skill['id'], 'name': skill['name'], 'content': content,
                            'sha256': hashlib.sha256(content.encode()).hexdigest()})
            source_map.append({'skill_id': skill['id'], 'name': skill['name'],
                               'source_ids': refs,
                               'families': sorted({self.plan['sources'][sid]['family'] for sid in refs}),
                               'trajectories': [self.plan['sources'][sid] for sid in refs]})
        atomic(self.root / 'catalog.json', catalog)
        atomic(self.root / 'skill-sources.json', source_map)
        atomic(self.root / 'completion.json', {'complete': True, 'skills': len(catalog),
            'windows': len(self.plan['windows']), 'sources': len(allowed), 'catalog_sha256': digest(catalog),
            'source_map_sha256': digest(source_map), 'teacher': 'gpt-6-astra', 'causal_effect_verified': False})
        print(json.dumps({'event': 'extraction_complete', 'skills': len(catalog), 'sources': len(allowed)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prompt', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--timeout', type=int, default=1200)
    parser.add_argument('--teacher-backend', choices=('api', 'codex-account'), default='codex-account')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    prompt = args.prompt.read_text()
    if (args.output / 'input-index.json').exists():
        plan = load(args.output / 'input-index.json')
        if plan['prompt_sha256'] != file_sha(args.prompt):
            raise ValueError('Prompt changed; use a new extraction directory')
        for relative, sha in plan['source_files_sha256'].items():
            if file_sha(args.source / relative) != sha:
                raise ValueError('Source changed')
    else:
        plan = prepare(args.source.resolve(), args.output.resolve(), args.prompt, 40000)
    if args.teacher_backend == 'codex-account':
        from skillrl.account_teacher import AccountTeacher
        class NativeExtractor(AccountTeacher, Extractor):
            pass
        NativeExtractor(args.output.resolve(), prompt, plan, args.timeout, validate).run(args.workers)
    else:
        Extractor(args.output.resolve(), prompt, plan, args.timeout).run(args.workers)


if __name__ == '__main__':
    main()
