import json
from pathlib import Path
import tempfile
import unittest
from skillrl.export_campaign import export_campaign
from skillrl.storage import atomic


class ReleaseCriteria(unittest.TestCase):
    def test_unknown_and_incomplete_are_preserved_but_excluded_from_success_training_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaign = root / 'campaign'
            family = 'memory_ability/fixture'
            atomic(campaign / 'campaign-plan.json', {'families': [family], 'upstream_commit': 'fixture'})
            atomic(campaign / 'campaign-summary.json', {'complete': False})
            rows = []
            for name, score, complete in [('valid', 1, True), ('incomplete', 1, False), ('unknown', None, True)]:
                run = campaign / 'families' / family / 'episodes' / name
                task = run / 'episodes' / name
                atomic(run / 'report.json', {'cost': {}})
                atomic(run / 'library/state.json', {'active': {}, 'processed': {}})
                atomic(task / 'execution.json', {'complete': True, 'events': [], 'final': 'fixture answer'})
                atomic(task / 'exposure.json', [])
                atomic(task / 'actor/wire/0000/request.json', {'model': 'gpt-6.1-sol', 'input': []})
                response = {'type': 'response.completed', 'response': {'model': 'gpt-6.1-sol', 'output': []}}
                (task / 'actor/wire/0000/response.raw').write_text('data: ' + json.dumps(response) + '\n')
                rows.append({'episode': name, 'task_id': name, 'report': str(run / 'report.json'),
                    'complete': complete, 'objective': {'score': score, 'details': {'task_score': score}}})
            atomic(campaign / 'families' / family / 'family-summary.json', {'episodes': rows})
            result = export_campaign(campaign, root / 'export')
            self.assertEqual(result['successful_training_trajectories'], 1)
            self.assertEqual(result['totals']['unknown'], 1)
            self.assertEqual(len((root / 'export/all-trajectories.jsonl').read_text().splitlines()), 3)
            accepted = json.loads((root / 'export/successful-trajectories.jsonl').read_text())
            self.assertEqual(accepted['task_id'], 'valid')
            self.assertTrue((root / 'export/RESULTS.md').exists())
            self.assertTrue((root / 'export/traces' / family / 'unknown.md').exists())


if __name__ == '__main__':
    unittest.main()
