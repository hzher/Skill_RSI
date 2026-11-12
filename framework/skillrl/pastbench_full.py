"""Full-suite PAST-Bench adapter using upstream manifests, services and grading."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from .pastbench import PastBenchmark, PastEnvironment
from .storage import atomic, canonical, file_sha, files, immutable, load
from .vendor.library import DeltaLibrary


def now():
    return datetime.now(timezone.utc).isoformat()


def import_seed(root, directory, sequence):
    """Import public runtime seeds once per exact source; do not inspect oracle files."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError('Seed directory absent: ' + str(directory))
    marker = root / 'seed-imports.json'
    imported = load(marker) if marker.exists() else {}
    identity = hashlib.sha256(canonical(files(directory))).hexdigest()
    if identity in imported:
        return
    memory_path, sessions_path = root / 'memory.json', root / 'sessions.json'
    memory = load(memory_path) if memory_path.exists() else []
    for p in sorted(directory.glob('memories/*.md')):
        from past_bench.runner.self_evolve import _split_memory_entries
        for entry in _split_memory_entries(p.read_text()):
            if entry not in memory:
                memory.append(entry)
    sessions = load(sessions_path) if sessions_path.exists() else []
    session_files = list(directory.glob('sessions/*.json')) + list(directory.glob('session_seed.json'))
    existing = {s.get('session_id', s.get('id')) for s in sessions}
    for p in sorted(session_files):
        data = load(p)
        candidates = data.get('sessions', [data]) if isinstance(data, dict) else data
        for session in candidates:
            sid = session.get('session_id', session.get('id'))
            if sid not in existing:
                sessions.append(session)
                existing.add(sid)
    library = DeltaLibrary(root / 'library', sequence)
    operations = []
    from .vendor.library_base import parse_skill
    for p in sorted(directory.glob('skills/**/SKILL.md')):
        content = p.read_text()
        if content.startswith('---'):
            name = parse_skill(content)[0]['name']
        else:
            name = re.sub('[^a-z0-9-]+', '-', p.parent.name.lower()).strip('-')
            description = next((s.strip('# ') for s in content.splitlines() if s.strip()), name)
            content = '---\nname: ' + name + '\ndescription: ' + json.dumps(description) + '\n---\n' + content
        if name not in library.state()['active']:
            operations.append({'name': 'new_skill_insert', 'arguments': {'skill_name': name, 'content': content}})
    if operations:
        library.apply('public-seed-' + identity[:20], operations)
    atomic(memory_path, memory)
    atomic(sessions_path, sessions)
    imported[identity] = {'source': str(directory), 'files': files(directory), 'imported_at': now(),
                          'skill_rendering': 'Original body; frontmatter added only when absent'}
    atomic(marker, imported)


def prepare_seeds(source, root, definition, episode_spec, sequence):
    for field in ('initial_home_fixture_dir', 'preseed_artifacts_dir'):
        value = episode_spec.get(field)
        if value:
            import_seed(root, Path(value) if Path(value).is_absolute() else source / value, sequence)
    if definition.hermes_home_seed:
        # Upstream pure materializer handles relative memory/skill/session seed references.
        from past_bench.runner.self_evolve import materialize_task_hermes_seed
        target = root / 'public-seed-overlay'
        if not target.exists():
            materialize_task_hermes_seed(task=definition, target_dir=target)
        import_seed(root, target, sequence)


def archive_session(root, task, execution):
    messages = [{'role': 'user', 'content': task.instruction, 'timestamp': now()}]
    for e in execution['events']:
        if e['type'] != 'tool':
            continue
        timestamp = e.get('timestamp', now())
        messages.extend([{'role': 'assistant', 'timestamp': timestamp, 'tool_calls': [{
            'id': e['call_id'], 'type': 'function', 'function': {'name': e['name'],
            'arguments': json.dumps(e['arguments'], ensure_ascii=False)}}]},
            {'role': 'tool', 'timestamp': timestamp, 'tool_call_id': e['call_id'],
             'content': json.dumps(e['result'], ensure_ascii=False)}])
    messages.append({'role': 'assistant', 'content': execution['final'], 'timestamp': now()})
    current = {'session_id': task.id, 'source': 'actual-codex-execution', 'title': task.instruction[:160], 'messages': messages}
    atomic(root / 'session-current.json', current)
    path = root / 'sessions.json'
    previous = load(path) if path.exists() else []
    if task.id not in {s.get('session_id', s.get('id')) for s in previous}:
        atomic(path, previous + [current])
    return current


class RecordedJudge:
    def __init__(self, model, root):
        from past_bench.graders.llm_judge import LLMJudge
        self.index = 0
        outer = self

        def create(**kwargs):
            index = outer.index
            outer.index += 1
            response = model.call(kwargs['messages'], [], root / f'{index:04d}')
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=response['message'].get('content')))])

        self.judge = LLMJudge.__new__(LLMJudge)
        self.judge.model_id = model.model
        self.judge.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def evaluate(self, *args):
        result = self.judge.evaluate(*args)
        if result is None:
            raise ValueError('Original LLM judge exhausted attempts; cannot invent a score')
        return result


def artifact(memory, snapshot):
    return {'memory_entries': memory, 'user_entries': [], 'memory_file_exists': bool(memory),
            'user_file_exists': False, 'skill_count': len(snapshot),
            'skill_docs': {s['name']: {'content': s['content'], 'preview': s['content'],
                                     'sha1': hashlib.sha1(s['content'].encode()).hexdigest()} for s in snapshot}}


class FullBenchmark(PastBenchmark):
    evaluate_after_curation = True

    def __init__(self, source, episode, public_dir, root, episode_spec, models):
        super().__init__(source, episode, public_dir, full=True)
        self.root, self.spec, self.models = Path(root), episode_spec, models

    def configuration(self):
        return {**super().configuration(), 'adapter': 'pastbench-full-v1', 'episode_contract': self.spec,
                'score_order': 'executor -> score-blind curator -> original grader -> skill reviewer',
                'skill_write_actor': 'framework Curator, never attributed to native Codex tools'}

    def prepare(self, task, workspace):
        environment = super().prepare(task, workspace)
        from past_bench.runner.self_evolve import resolve_episode_tool_config
        environment.persistence_flags = resolve_episode_tool_config(persistence_enabled=self.spec['persistence_allowed'],
            expected_signal=self.spec['mechanism'], memory_enabled=True, user_profile_enabled=True,
            skills_enabled=True, session_search_enabled=True)
        environment.sessions_path = self.root / 'sessions.json'
        return environment

    def evaluate(self, task, workspace, execution):
        from past_bench.models.trace import TraceStart, TraceMessage, TraceEnd, ToolDispatch, AuditSnapshot
        from past_bench.models.message import Message
        from past_bench.graders.registry import get_grader
        from past_bench.runner.self_evolve import grade_episode, _extract_internal_tool_usage
        current = archive_session(self.root, task, execution)
        # Preserve actual source roles while mapping reads/writes to upstream's substrate contract.
        normalized = json.loads(json.dumps(current))
        for message in normalized['messages']:
            for call in message.get('tool_calls', []):
                if call['function']['name'] == 'Skill':
                    call['function']['name'] = 'skill_view'
        curator = self.root / 'episodes' / task.id / 'curator'
        commit = load(curator / 'commit.json') if (curator / 'commit.json').exists() else {}
        if self.spec['persistence_allowed']:
            for i, operation in enumerate(commit.get('applied_ops', [])):
                action = 'create' if operation['op'] == 'new_skill_insert' else 'edit'
                normalized['messages'].append({'role': 'assistant', 'timestamp': now(),
                    'source_actor': 'framework_curator', 'tool_calls': [{'id': 'curator-' + str(i),
                    'function': {'name': 'skill_manage', 'arguments': json.dumps({'action': action,
                    'name': operation.get('name'), 'source_actor': 'framework_curator'})}}]})
        native_session = self.root / 'evaluation' / task.id / 'objective' / 'normalized-session.json'
        atomic(native_session, normalized)
        internal = _extract_internal_tool_usage(native_session)
        before = artifact(execution['memory_before'], execution['library_at_start'])
        from .pipeline import Pipeline
        sequence = load(self.root / 'library/state.json')['sequence_id']
        after_skills = Pipeline.snapshot(None, DeltaLibrary(self.root / 'library', sequence))
        after = artifact(execution['memory_after'], after_skills if self.spec['persistence_allowed'] else execution['library_at_start'])
        after['internal_tools'] = internal
        atomic(native_session.parent / 'artifact-before.json', before)
        atomic(native_session.parent / 'artifact-after.json', after)
        trace = [TraceStart(trace_id=task.id, task_id=task.id, model=self.models['executor'].model),
                 TraceMessage(trace_id=task.id, message=Message(role='user', content=task.instruction))]
        for e in execution['events']:
            if e['type'] == 'tool' and e['name'] not in ('Skill', 'memory', 'session_search'):
                trace.append(ToolDispatch(trace_id=task.id, tool_use_id=e['call_id'], tool_name=e['name'],
                    endpoint_url=e['endpoint_url'], request_body=e['arguments'], response_status=e['response_status'],
                    response_body=e['result'], latency_ms=e['latency_ms'], timestamp=e.get('timestamp', now())))
        trace.append(TraceMessage(trace_id=task.id, message=Message(role='assistant', content=execution['final'])))
        for name, audit in execution['environment']['audit'].items():
            trace.append(AuditSnapshot(trace_id=task.id, service_name=name, audit_url='original-mock-service', audit_data=audit))
        usage = execution.get('usage', {})
        completed_requests = len(list((self.root / 'episodes' / task.id / 'actor/wire').glob('*/result.json')))
        inp, out = usage.get('input_tokens', 0), usage.get('output_tokens', 0)
        trace.append(TraceEnd(trace_id=task.id, input_tokens=inp, output_tokens=out,
            model_input_tokens=inp, model_output_tokens=out, total_tokens=inp + out,
            total_turns=completed_requests, wall_time_s=execution.get('wall_seconds', 0),
            tool_time_s=sum(e.get('latency_ms', 0) for e in execution['events']) / 1000))
        trace_path = native_session.parent / 'native-trace.jsonl'
        immutable(trace_path, ''.join(e.model_dump_json() + '\n' for e in trace).encode())
        grader = get_grader(task.id, task_dir=self.episode)
        judge = RecordedJudge(self.models['judge'], native_session.parent / 'judge-requests')
        result = grade_episode(trace_path=trace_path, task=self.definition, tasks_dir=self.source / 'self-evolve-tasks-v2',
            task_dir=self.episode, grader=grader, judge=judge, env_snapshot={}, artifact_before=before,
            artifact_summary=after, expected_persistence_signal=self.spec['expected_persistence_signal'])
        atomic(native_session.parent / 'original-grade.json', result)
        return {'score': int(result['passed']), 'details': {**result,
            'skill_write_actor': 'framework Curator; mapped from actual applied_ops',
            'adapter': 'Codex/frame pipeline; original grader, judge and hard-pass helpers; not Hermes runtime'}}


def inherit(parent, root):
    root.mkdir(parents=True, exist_ok=True)
    if parent is None:
        return
    for name in ('library', 'memory.json', 'sessions.json', 'seed-imports.json'):
        src, dst = parent / name, root / name
        if src.exists() and not dst.exists():
            shutil.copytree(src, dst) if src.is_dir() else shutil.copyfile(src, dst)


def run_full_family(source, family, config, output, *, continue_on_error=True):
    import yaml
    from .pipeline import Pipeline
    from .codex import CodexExecutor
    from .curation import Curator
    from .review import Reviewer
    from .models import HTTPModel
    source, output = Path(source).resolve(), Path(output).resolve()
    sys.path.insert(0, str(source / 'src'))
    from past_bench.self_evolve_v2.manifest import generate_manifest
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / 'upstream-manifest.yaml'
    if not manifest.exists():
        generate_manifest(family, manifest, repo_root=source)
    specifications = yaml.safe_load(manifest.read_text())['episodes']
    family_path = source / 'self-evolve-tasks-v2' / family
    family_doc = yaml.safe_load((family_path / 'family.yaml').read_text())
    public = output / 'public-empty'
    public.mkdir(exist_ok=True)
    plan = {'family': family, 'config': config, 'adapter': 'full-v1',
            'upstream_commit': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
            'family_yaml_sha256': file_sha(family_path / 'family.yaml'), 'manifest_sha256': file_sha(manifest)}
    immutable(output / 'family-plan.json', canonical(plan))
    models = {role: HTTPModel(**spec) for role, spec in config['models'].items()}
    rows, previous = [], None
    if config.get('episode_limit'):
        specifications = specifications[:config['episode_limit']]
    for specification in specifications:
        episode_path = Path(specification['task'])
        name = episode_path.name
        root = output / 'episodes' / name
        mode = specification['history_mode']
        parent = previous if mode == 'continue' else (
            output / 'anchors' / specification['history_load_anchor'] if mode == 'from_anchor' else None)
        if not (root / 'report.json').exists():
            inherit(parent, root)
        benchmark = FullBenchmark(source, episode_path, public, root, specification, models)
        if not (root / 'plan.json').exists():
            prepare_seeds(source, root, benchmark.definition, specification, family_doc['family_id'])
        action_tools = [t.name for t in benchmark.definition.tools if any(x in t.name.lower() for x in
            ('share', 'update', 'create', 'send', 'action', 'notify', 'plonk'))] or [t.name for t in benchmark.definition.tools]
        print(json.dumps({'event': 'episode_start', 'family': family, 'episode': name}), flush=True)
        pipeline = Pipeline(root, benchmark, CodexExecutor(models['executor'], **config.get('codex', {})),
            Curator(models['curator'], **config.get('curator', {})),
            Reviewer(models['judge'], action_tools=action_tools), sequence=family_doc['family_id'])
        try:
            report = pipeline.run()
        except Exception as exc:
            atomic(root / 'driver-error.json', {'type': type(exc).__name__, 'message': str(exc)[:1500]})
            report = {'complete': False, 'active_skills': len(DeltaLibrary(root / 'library', family_doc['family_id']).state()['active'])}
        task_id = benchmark.task.id
        objective_path = root / 'evaluation' / task_id / 'objective/result.json'
        objective = load(objective_path) if objective_path.exists() else None
        execution_path = root / 'episodes' / task_id / 'execution.json'
        row = {'episode': name, 'task_id': task_id, 'branch': mode, 'report': str(root / 'report.json'),
               'complete': report['complete'], 'execution_complete': execution_path.exists(),
               'objective': objective, 'active_skills': report['active_skills']}
        rows.append(row)
        atomic(output / 'family-progress.json', rows)
        print(json.dumps({'event': 'episode_done', 'family': family, **row}), flush=True)
        if execution_path.exists() and specification['persistence_allowed']:
            archive_session(root, benchmark.task, load(execution_path))
        previous = root
        if specification.get('history_save_anchor'):
            anchor = output / 'anchors' / specification['history_save_anchor']
            if not anchor.exists():
                inherit(root, anchor)
        if not row['complete'] and not continue_on_error:
            break
    summary = {'family': family, 'episodes': rows, 'attempts_finished': len(rows) == len(specifications),
        'complete': len(rows) == len(specifications) and all(r['complete'] for r in rows),
        'passed': sum(bool(r['objective'] and r['objective']['score'] == 1) for r in rows),
        'known_scores': sum(bool(r['objective'] and r['objective']['score'] is not None) for r in rows),
        'weights_updated': False, 'score_contract': 'upstream grade_episode (including notes_memory hard_pass)'}
    atomic(output / 'family-summary.json', summary)
    return summary
