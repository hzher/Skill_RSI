"""PAST-Bench notes-family adapter and branch-aware data generation driver.

Upstream task prompts, mock services and graders are used without modification.
The first supported substrate is notes + versioned Markdown skills, not Hermes state.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from .benchmarks import Task
from .storage import atomic, copy_public, file_sha, files, load, immutable, canonical


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def rewrite_url(url, port):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, '127.0.0.1:' + str(port), parsed.path, parsed.query, ''))


class PastEnvironment:
    def __init__(self, source, definition, workspace):
        self.source, self.definition, self.workspace = source, definition, workspace
        self.processes, self.tools, self.audits = [], [], {}
        self.memory_path = workspace.parents[2] / 'memory.json'

    def start(self):
        ports = {}
        try:
            for service in self.definition.services:
                port = free_port()
                ports[service.port] = port
                module = service.command.split()[1].replace('/', '.').removesuffix('.py')
                env = {k: v for k, v in os.environ.items()
                       if not any(x in k.upper() for x in ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}
                env.update({k: str((self.source / v).resolve()) if (self.source / v).exists() else v
                            for k, v in service.env.items()})
                log = (self.workspace.parent / ('service-' + service.name + '.log')).open('w')
                proc = subprocess.Popen([sys.executable, '-m', 'uvicorn', module + ':app',
                    '--host', '127.0.0.1', '--port', str(port)], cwd=self.source, env=env,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                self.processes.append((service, proc, log, port))
                health = rewrite_url(service.health_check, port)
                for attempt in range(100):
                    if proc.poll() is not None:
                        raise RuntimeError('Original mock service exited: ' + service.name)
                    try:
                        request = urllib.request.Request(health, data=b'{}' if service.health_check_method == 'POST' else None,
                            method=service.health_check_method, headers={'Content-Type': 'application/json'})
                        with urllib.request.urlopen(request, timeout=1):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('Original service readiness timeout')
                # Health requests must not count as agent grounding.
                if service.reset_endpoint:
                    req = urllib.request.Request(rewrite_url(service.reset_endpoint, port), data=b'{}',
                        headers={'Content-Type': 'application/json'})
                    with urllib.request.urlopen(req, timeout=5):
                        pass
            endpoints = {endpoint.tool_name: endpoint for endpoint in self.definition.tool_endpoints}
            for tool in self.definition.tools:
                endpoint = endpoints[tool.name]
                original_port = urlsplit(endpoint.url).port
                self.tools.append({'name': tool.name, 'description': tool.description,
                    'inputSchema': tool.input_schema, 'url': rewrite_url(endpoint.url, ports[original_port]),
                    'method': endpoint.method})
            return self
        except Exception:
            self.close()
            raise

    def codex_tools(self):
        return self.tools

    def metadata(self):
        for service, proc, log, port in self.processes:
            with urllib.request.urlopen('http://127.0.0.1:' + str(port) + '/' + service.name + '/audit', timeout=5) as response:
                self.audits[service.name] = json.load(response)
        return {'backend': 'past-bench-original-services', 'audit': self.audits,
                'isolation': 'native read-only harness; benchmark tools over loopback MCP; not a container security boundary'}

    def close(self):
        receipts = []
        for service, proc, log, port in self.processes:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            log.close()
            receipts.append({'service': service.name, 'pid': proc.pid, 'port': port, 'exit_code': proc.returncode})
        atomic(self.workspace.parent / 'environment-stop.json', receipts)


class PastBenchmark:
    def __init__(self, source, episode, public_dir):
        self.source, self.episode = Path(source).resolve(), Path(episode).resolve()
        sys.path.insert(0, str(self.source / 'src'))
        from past_bench.models.task import TaskDefinition
        # Fail before any generation if upstream grader imports/dependencies are unavailable.
        from past_bench.graders.self_evolve_helpers import SelfEvolveTaskGrader
        self.definition = TaskDefinition.from_yaml(self.episode / 'task.yaml')
        # Other substrates require workspace/seed/session adapters. Reject rather than silently drop inputs.
        if {s.name for s in self.definition.services} != {'notes'}:
            raise ValueError('Initial adapter supports original notes-service families only')
        if self.definition.prompt.attachments or self.definition.sandbox_files or self.definition.hermes_home_seed:
            raise ValueError('This episode needs an additional public workspace or persistence adapter')
        self.task = Task(self.definition.task_id, self.definition.prompt.text, Path(public_dir))

    def tasks(self):
        return [self.task]

    def configuration(self):
        return {'kind': 'past-bench', 'upstream_commit': subprocess.check_output(
            ['git', '-C', str(self.source), 'rev-parse', 'HEAD'], text=True).strip(),
            'episode': str(self.episode.relative_to(self.source)),
            'task_yaml_sha256': file_sha(self.episode / 'task.yaml'),
            'grader_sha256': file_sha(self.episode / 'grader.py'),
            'expectations_sha256': file_sha(self.episode / 'expectations.json'),
            'fixture_files': files(self.episode / 'fixtures')}

    def prepare(self, task, workspace):
        copy_public(task.public_dir, workspace)
        return PastEnvironment(self.source, self.definition, workspace)

    def evaluate(self, task, workspace, execution):
        from past_bench.models.message import Message
        from past_bench.models.trace import TraceMessage, ToolDispatch
        from past_bench.models.scoring import compute_task_score, is_pass
        module_spec = importlib.util.spec_from_file_location('past_episode_grader', self.episode / 'grader.py')
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        messages = [TraceMessage(trace_id=task.id, message=Message(role='user', content=task.instruction)),
                    TraceMessage(trace_id=task.id, message=Message(role='assistant', content=execution['final']))]
        dispatches = [ToolDispatch(trace_id=task.id, tool_use_id=e['call_id'], tool_name=e['name'],
            endpoint_url=e['endpoint_url'], request_body=e['arguments'], response_status=e['response_status'],
            response_body=e['result'], latency_ms=e['latency_ms'])
            for e in execution['events'] if e['type'] == 'tool' and e['name'] not in ('Skill', 'memory')]
        memory_calls = [e for e in execution['events'] if e.get('name') == 'memory' and not e.get('error')]
        reads = sum(e['arguments']['action'] == 'read' for e in memory_calls)
        writes = sum(e['arguments']['action'] != 'read' for e in memory_calls)
        before, after = execution['memory_before'], execution['memory_after']
        expectations = load(self.episode / 'expectations.json')
        keywords = expectations.get('require_rule_artifact', [])
        # Match upstream's memory-injection definition: a real inherited memory artifact
        # included in this fresh session. No skill-read is counted as a memory-write.
        mechanism = {'memory_read_count': reads, 'memory_write_count': writes,
            'memory_injection_count': int(bool(before)), 'used_expected_signal': bool(before) or reads > 0,
            'skill_read_count': sum(e.get('name') == 'Skill' and not e.get('error') for e in execution['events']),
            'artifact_rule_quality_keywords': [k for k in keywords if k.lower() in '\n'.join(after).lower()]}
        env_snapshot = {'_self_evolve_internal_tools': {'memory_calls': len(memory_calls)},
            '_self_evolve_mechanism_metrics': mechanism, '_self_evolve_has_memory_artifact': bool(after),
            '_self_evolve_memory_entries': after, '_self_evolve_skill_count': len(execution['skills_at_start']),
            '_self_evolve_skill_docs': {s['name']: s['content'] for s in execution['skills_at_start']}}
        # Original deterministic completion/robustness/safety scoring. Communication LLM is unused by task_score.
        scores = module.TaskGrader().grade(messages, dispatches, self.definition,
            audit_data=execution['environment']['audit'], judge=None, env_snapshot=env_snapshot)
        score = compute_task_score(scores)
        return {'score': int(is_pass(score)), 'details': {'task_score': score,
                'dimensions': scores.model_dump(), 'passed': is_pass(score),
                'communication_judge': 'not_run; excluded by upstream task_score formula',
                'mechanism_scope': 'actual Codex MCP memory writes/reads and inherited prompt injection; not Hermes runtime',
                'mechanism_metrics': mechanism}}


def run_family(source, family, config, output):
    import yaml
    from .pipeline import Pipeline
    from .codex import CodexExecutor
    from .curation import Curator
    from .review import Reviewer
    from .models import HTTPModel
    source, output = Path(source).resolve(), Path(output).resolve()
    family_path = source / 'self-evolve-tasks-v2' / family
    family_spec = yaml.safe_load((family_path / 'family.yaml').read_text())
    output.mkdir(parents=True, exist_ok=True)
    public = output / 'public-empty'
    public.mkdir(exist_ok=True)
    sequence = family_spec['family_id']
    plan = {'family': family, 'family_yaml_sha256': file_sha(family_path / 'family.yaml'),
            'upstream_commit': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
            'config': config}
    immutable(output / 'family-plan.json', canonical(plan))
    models = {role: HTTPModel(**spec) for role, spec in config['models'].items()}
    anchors = {a['save_after']: a['name'] for a in family_spec['history_plan'].get('anchors', [])}
    current = None
    rows = []
    # Preflight every original episode before making any model call.
    for branch in family_spec['history_plan']['branches']:
        for name in branch['episodes']:
            PastBenchmark(source, family_path / name, public)
    for branch in family_spec['history_plan']['branches']:
        for name in branch['episodes']:
            root = output / 'episodes' / name
            if not (root / 'library').exists():
                parent = current if branch['mode'] == 'continue' else (
                    output / 'anchors' / branch['anchor'] / 'library' if branch['mode'] == 'from_anchor' else None)
                if parent:
                    root.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(parent, root / 'library')
                    parent_memory = parent.parent / 'memory.json'
                    if parent_memory.exists():
                        shutil.copyfile(parent_memory, root / 'memory.json')
            bench = PastBenchmark(source, family_path / name, public)
            pipeline = Pipeline(root, bench, CodexExecutor(models['executor'], **config.get('codex', {})),
                Curator(models['curator'], **config.get('curator', {})),
                Reviewer(models['judge'], action_tools=[t.name for t in bench.definition.tools if
                    any(x in t.name for x in ('share', 'update', 'create', 'send'))]), sequence=sequence)
            print(json.dumps({'event': 'episode_start', 'family': family, 'episode': name, 'branch': branch['mode']}), flush=True)
            report = pipeline.run()
            objective = root / 'evaluation' / bench.task.id / 'objective' / 'result.json'
            row = {'episode': name, 'task_id': bench.task.id, 'branch': branch['mode'],
                   'report': str(root / 'report.json'), 'complete': report['complete'],
                   'objective': load(objective) if objective.exists() else None,
                   'active_skills': report['active_skills']}
            rows.append(row)
            atomic(output / 'family-progress.json', rows)
            print(json.dumps({'event': 'episode_done', **row}), flush=True)
            if not report['complete'] or row['objective'] is None or row['objective']['score'] is None:
                raise RuntimeError('Incomplete episode held; no automatic replay or downstream propagation: ' + name)
            if branch['mode'] == 'continue':
                current = root / 'library'
            if name in anchors:
                target = output / 'anchors' / anchors[name] / 'library'
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(root / 'library', target)
                    if (root / 'memory.json').exists():
                        shutil.copyfile(root / 'memory.json', target.parent / 'memory.json')
    summary = {'family': family, 'episodes': rows, 'complete': True, 'weights_updated': False,
               'passed': sum(r['objective']['score'] == 1 for r in rows),
               'known_scores': sum(r['objective']['score'] is not None for r in rows)}
    atomic(output / 'family-summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--family', default='memory_ability/SM01_preference_adoption')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    summary = run_family(args.source, args.family, load(args.config), args.output)
    print(json.dumps({'complete': summary['complete'], 'episodes': len(summary['episodes']), 'passed': summary['passed']}))


if __name__ == '__main__':
    main()
