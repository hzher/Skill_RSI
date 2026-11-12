import json
from pathlib import Path
import tempfile
import unittest
from skillrl.export_pastbench import export
from skillrl.storage import atomic, load


class ExportContracts(unittest.TestCase):
    def fixture(self, root, score=1, model=None):
        run = root / 'family'
        episode = run / 'episodes/learn'
        task = episode / 'episodes/task'
        atomic(run / 'family-plan.json', {'upstream_commit': 'fixed'})
        atomic(run / 'family-summary.json', {'complete': True, 'family': 'sample', 'episodes': [{
            'episode': 'learn', 'task_id': 'task', 'branch': 'continue', 'report': str(episode / 'report.json'),
            'complete': True, 'objective': {'score': score, 'details': {}}}]})
        atomic(episode / 'plan.json', {'runtime_source_sha256': {'source.py': 'sha'}})
        atomic(episode / 'library/state.json', {'active': {}})
        atomic(task / 'execution.json', {'complete': True, 'events': [
            {'type': 'tool', 'name': 'notes_share', 'error': False}], 'skills_at_start': []})
        atomic(task / 'exposure.json', [])
        atomic(task / 'actor/command.json', {'cli_version': 'fixture'})
        wire = task / 'actor/wire/0000'
        atomic(wire / 'request.json', {'model': 'gpt-6.1-sol', 'input': []})
        response = {'output': [], 'model': model} if model else {'output': []}
        (wire / 'response.raw').write_text('data: ' + json.dumps({'type': 'response.completed', 'response': response}) + '\n')
        return run

    def test_missing_reported_model_remains_missing_not_filled_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = export(self.fixture(root), root / 'export')
            self.assertEqual(summary['successful_trajectories'], 1)
            row = json.loads((root / 'export/successful-trajectories.jsonl').read_text())
            self.assertFalse(row['native_wire'][0]['reported_model_present'])
            self.assertNotIn('model', row['native_wire'][0]['response'])
            self.assertFalse(row['policy_gradient_eligible'])

    def test_null_is_preserved_and_excluded_from_success_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = export(self.fixture(root, score=None), root / 'export')
            self.assertEqual(summary['successful_trajectories'], 0)
            row = json.loads((root / 'export/all-trajectories.jsonl').read_text())
            self.assertIsNone(row['objective']['score'])

    def test_explicit_reported_model_mismatch_rejects_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                export(self.fixture(root, model='different-model'), root / 'export')


if __name__ == '__main__':
    unittest.main()
