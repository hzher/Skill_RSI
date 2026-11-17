import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skillrl.collect import run_collection, select_tasks


class CollectionTests(unittest.TestCase):
    def plan(self):
        tasks = [
            {'id': 'database_exploration-000', 'family': 'database_exploration', 'canonical_index': 0},
            {'id': 'codebase_adaptation-001', 'family': 'codebase_adaptation', 'canonical_index': 1},
        ]
        return {'selected_tasks': tasks, 'grouped_order': [t['id'] for t in tasks],
                'shuffled_order': [t['id'] for t in reversed(tasks)]}

    def test_persistent_orders_preserve_same_task_set(self):
        plan = self.plan()
        grouped = select_tasks(plan, 'grouped')
        shuffled = select_tasks(plan, 'shuffled')
        self.assertEqual(grouped, list(reversed(shuffled)))
        self.assertEqual(select_tasks({'tasks': grouped}, 'independent'), grouped)

    def test_invalid_input_rejected_before_any_execution(self):
        plan = self.plan()
        plan['shuffled_order'][1] = plan['shuffled_order'][0]
        with self.assertRaises(ValueError):
            select_tasks(plan, 'shuffled')
        for value in [-1, True, '0']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    select_tasks({'tasks': [{'id': 'x', 'family': 'database_exploration',
                                             'canonical_index': value}]}, 'independent')

    def test_persistent_dispatch_uses_original_chain(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {'repository': temp, 'model': 'test'}
            with patch('skillrl.collect.execute_chain', return_value={'complete': True}) as chain:
                with patch('skillrl.collect.execute') as independent:
                    result = run_collection(config, self.plan(), 'shuffled', Path(temp) / 'run')
            self.assertTrue(result['complete'])
            self.assertEqual(chain.call_args.args[1], select_tasks(self.plan(), 'shuffled'))
            independent.assert_not_called()

    def test_independent_tasks_have_separate_paths_and_continue_after_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {'repository': temp, 'model': 'test'}
            tasks = self.plan()['selected_tasks']
            with patch('skillrl.collect.execute', side_effect=[RuntimeError('unresolved'), {'complete': True}]) as execute:
                with patch('skillrl.collect.execute_chain') as chain:
                    result = run_collection(config, {'tasks': tasks}, 'independent', Path(temp) / 'run')
            self.assertFalse(result['complete'])
            self.assertEqual(result['completed_tasks'], 1)
            self.assertEqual(execute.call_count, 2)
            paths = [call.args[3] for call in execute.call_args_list]
            self.assertEqual(len(set(paths)), 2)
            self.assertEqual(result['records'][0]['error']['type'], 'RuntimeError')
            chain.assert_not_called()

    def test_manifest_change_cannot_reuse_existing_run(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {'repository': temp, 'model': 'test'}
            output = Path(temp) / 'run'
            plan = {'tasks': self.plan()['selected_tasks']}
            with patch('skillrl.collect.execute', return_value={'complete': True}):
                self.assertTrue(run_collection(config, plan, 'independent', output)['complete'])
            plan['tasks'].reverse()
            with patch('skillrl.collect.execute') as execute:
                with self.assertRaises(ValueError):
                    run_collection(config, plan, 'independent', output)
                execute.assert_not_called()

    def test_shipped_inputs_have_100_300_and_301_unique_ids(self):
        root = Path(__file__).resolve().parents[1] / 'examples' / 'clbench'
        paired = json.loads((root / 'paired-100.json').read_text())
        grouped = select_tasks(paired, 'grouped')
        shuffled = select_tasks(paired, 'shuffled')
        self.assertEqual(len(grouped), 100)
        self.assertEqual({t['id'] for t in grouped}, {t['id'] for t in shuffled})
        for size in [300, 301]:
            tasks = select_tasks(json.loads((root / f'independent-{size}.json').read_text()), 'independent')
            self.assertEqual(len(tasks), size)
            self.assertEqual(len({t['id'] for t in tasks}), size)


if __name__ == '__main__':
    unittest.main()
