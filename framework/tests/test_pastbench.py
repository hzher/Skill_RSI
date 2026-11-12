import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from skillrl.benchmarks import Task
from skillrl.pastbench import run_family
from skillrl.storage import atomic, load


class BranchContracts(unittest.TestCase):
    def fixture(self, folder):
        import yaml
        root = Path(folder)
        family = root / 'source/self-evolve-tasks-v2/memory/sample'
        family.mkdir(parents=True)
        spec = {'family_id': 'sample', 'history_plan': {
            'anchors': [{'name': 'learned', 'save_after': 'learn'}],
            'branches': [{'mode': 'continue', 'episodes': ['cold', 'learn']},
                         {'mode': 'from_anchor', 'anchor': 'learned', 'episodes': ['near', 'far']},
                         {'mode': 'fresh', 'episodes': ['control-a', 'control-b']}]}}
        (family / 'family.yaml').write_text(yaml.safe_dump(spec))
        model = {'base_url': 'http://127.0.0.1:1/v1', 'model': 'fixture', 'api_key_env': 'UNUSED'}
        return root / 'source', {'models': {k: model for k in ('executor', 'curator', 'judge')}}, root / 'run'

    def test_memory_and_skill_branches_share_anchor_but_not_consumers_or_controls(self):
        observed = {}

        class Benchmark:
            def __init__(self, source, episode, public):
                self.task = Task(episode.name, 'public', public)
                self.definition = SimpleNamespace(tools=[SimpleNamespace(name='notes_share')])

        class Pipeline:
            def __init__(self, root, bench, *args, **kwargs):
                self.root, self.bench = root, bench

            def run(self):
                root, name = self.root, self.bench.task.id
                library, memory = root / 'library', root / 'memory.json'
                observed[name] = {'library': load(library / 'marker.json') if library.exists() else [],
                                  'memory': load(memory) if memory.exists() else []}
                root.mkdir(parents=True, exist_ok=True)
                atomic(library / 'marker.json', observed[name]['library'] + [name])
                atomic(memory, observed[name]['memory'] + [name])
                atomic(root / 'evaluation' / name / 'objective/result.json', {'score': 1, 'details': {}})
                return {'complete': True, 'active_skills': 1}

        with tempfile.TemporaryDirectory() as folder:
            source, config, output = self.fixture(folder)
            with patch('skillrl.pastbench.PastBenchmark', Benchmark), patch('skillrl.pipeline.Pipeline', Pipeline), \
                 patch('skillrl.pastbench.subprocess.check_output', return_value='fixed-commit'):
                result = run_family(source, 'memory/sample', config, output)
            self.assertEqual(result['passed'], 6)
            self.assertEqual(observed['learn'], {'library': ['cold'], 'memory': ['cold']})
            self.assertEqual(observed['near'], {'library': ['cold', 'learn'], 'memory': ['cold', 'learn']})
            self.assertEqual(observed['far'], observed['near'])
            self.assertEqual(observed['control-a'], {'library': [], 'memory': []})
            self.assertEqual(observed['control-b'], observed['control-a'])

    def test_null_objective_stops_before_any_downstream_episode(self):
        calls = []

        class Benchmark:
            def __init__(self, source, episode, public):
                self.task = Task(episode.name, 'public', public)
                self.definition = SimpleNamespace(tools=[SimpleNamespace(name='notes_share')])

        class Pipeline:
            def __init__(self, root, bench, *args, **kwargs):
                self.root, self.bench = root, bench

            def run(self):
                calls.append(self.bench.task.id)
                atomic(self.root / 'evaluation' / self.bench.task.id / 'objective/result.json',
                       {'score': None, 'details': {'status': 'fixture_error'}})
                return {'complete': True, 'active_skills': 0}

        with tempfile.TemporaryDirectory() as folder:
            source, config, output = self.fixture(folder)
            with patch('skillrl.pastbench.PastBenchmark', Benchmark), patch('skillrl.pipeline.Pipeline', Pipeline), \
                 patch('skillrl.pastbench.subprocess.check_output', return_value='fixed-commit'):
                with self.assertRaises(RuntimeError):
                    run_family(source, 'memory/sample', config, output)
            self.assertEqual(calls, ['cold'])
            self.assertFalse((output / 'family-summary.json').exists())


if __name__ == '__main__':
    unittest.main()
