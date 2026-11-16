import tempfile
from pathlib import Path
import unittest
from skillrl.bounded_review import BoundedReviewer
from skillrl.benchmarks import Task


class NoCallModel:
    def settings(self):
        return {'fixture': True}

    def call(self, *args, **kwargs):
        raise AssertionError('Oversized auxiliary context must not be submitted')


class BoundedReview(unittest.TestCase):
    def test_oversized_context_retains_reward_but_never_invents_skill_credit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = {'skill_id': 's:1', 'name': 'procedure', 'raw_sha256': 'hash', 'content': 'body'}
            result = BoundedReviewer(NoCallModel(), context_limit=100).run(
                Task('t', 'public task', root), {'events': [{'type': 'tool', 'result': 'x' * 1000}]},
                [skill], [{'body_proven': True, 'proof': []}], {'score': 1, 'details': {}}, root / 'review')
            self.assertEqual(result['task_reward'], 1)
            self.assertFalse(result['context_hold']['model_called'])
            self.assertEqual(result['skills'][0]['status'], 'invalid_or_unresolved_review')
            self.assertIsNone(result['skills'][0]['training_credit'])


if __name__ == '__main__':
    unittest.main()
