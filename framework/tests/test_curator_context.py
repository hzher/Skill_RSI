import copy
import json
from pathlib import Path
import tempfile
import unittest

from skillrl.benchmarks import Task
from skillrl.curation import Curator
from skillrl.vendor.library import DeltaLibrary
from support import proposal


def call(name, args, ordinal):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': str(ordinal), 'function': {'name': name, 'arguments': json.dumps(args)}}]}


class ScriptedModel:
    def __init__(self, messages):
        self.replies = iter(messages)
        self.inputs = []

    def settings(self):
        return {'fixture': True}

    def call(self, messages, tools, path, **kwargs):
        self.inputs.append(copy.deepcopy(messages))
        return {'message': next(self.replies), 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}


class CuratorContext(unittest.TestCase):
    def test_oversized_read_is_not_forwarded_or_credited_but_excerpt_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);public = root / 'public';public.mkdir()
            model = ScriptedModel([
                call('Read', {'path': 'index.json'}, 1),
                call('Read', {'path': 'events/e00000.json'}, 2),
                call('SearchEvidence', {'path': 'events/e00000.json', 'query': 'source-marker'}, 3),
                call('submit_transaction', proposal('noop'), 4),
                {'role': 'assistant', 'content': 'Done.'}])
            result = Curator(model, execution_policy='verified-bounded-v1').run(
                Task('t', 'Public task.', public), {'events': [
                    {'type': 'tool', 'name': 'session_search', 'result': 'x' * 40000 + 'source-marker'}]},
                [], public, DeltaLibrary(root / 'library', 's'), root / 'curator')
            self.assertEqual(result['read_events'], [])
            self.assertEqual(result['status'], 'no_op')
            forwarded = json.dumps(model.inputs)
            self.assertNotIn('x' * 30000, forwarded)
            self.assertIn('source-marker', forwarded)
            self.assertIn('full_read_citation_credit', forwarded)
            self.assertIn('event_metadata', forwarded)

    def test_partial_excerpt_does_not_authorize_a_skill_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);public = root / 'public';public.mkdir()
            update = proposal();update['operations'][0]['evidence_ids'] = ['e00000']
            model = ScriptedModel([
                call('SearchEvidence', {'path': 'events/e00000.json', 'query': 'source-marker'}, 1),
                call('submit_transaction', update, 2),
                call('submit_transaction', proposal('noop'), 3),
                {'role': 'assistant', 'content': 'Done.'}])
            library = DeltaLibrary(root / 'library', 's')
            result = Curator(model, execution_policy='verified-bounded-v1').run(
                Task('t', 'Public task.', public), {'events': [
                    {'type': 'tool', 'result': 'source-marker' + 'x' * 40000}]},
                [], public, library, root / 'curator')
            self.assertEqual(result['status'], 'no_op')
            self.assertEqual(library.state()['active'], {})
            self.assertIn('Cite actual full evidence Reads', json.dumps(model.inputs))

    def test_total_context_guard_stops_before_an_oversized_provider_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);public = root / 'public';public.mkdir()
            reply = call('Read', {'path': 'index.json'}, 1)
            reply['content'] = 'x' * 170000
            model = ScriptedModel([reply])
            result = Curator(model, execution_policy='verified-bounded-v1').run(
                Task('t', 'Public task.', public), {'events': []}, [], public,
                DeltaLibrary(root / 'library', 's'), root / 'curator')
            self.assertEqual(result['status'], 'context_budget_exhausted')
            self.assertIsNone(result['commit'])
            self.assertEqual(len(model.inputs), 1)


if __name__ == '__main__':
    unittest.main()
