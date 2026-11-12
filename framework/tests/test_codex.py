import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skillrl.agents import codex_exposures
from skillrl.codex_mcp import serve
from skillrl.reporting import costs
from skillrl.storage import atomic


class CodexContracts(unittest.TestCase):
    def request(self, config, method, params):
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps({'jsonrpc': '2.0', 'id': 1,
                'method': method, 'params': params}) + '\n')), patch('sys.stdout', output):
            serve(config)
        return json.loads(output.getvalue())

    def test_memory_is_durable_and_replacement_requires_exact_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {'tools': [], 'memory': str(root / 'memory.json'), 'events': str(root / 'events.jsonl')}
            self.request(config, 'tools/call', {'name': 'memory', 'arguments': {'action': 'add', 'text': 'Use TSV'}})
            self.assertEqual(json.loads((root / 'memory.json').read_text()), ['Use TSV'])
            result = self.request(config, 'tools/call', {'name': 'memory', 'arguments': {
                'action': 'replace', 'target': 'unread imagined parent', 'text': 'Use prose'}})
            self.assertIn('error', result)
            self.assertEqual(json.loads((root / 'memory.json').read_text()), ['Use TSV'])
            self.request(config, 'tools/call', {'name': 'memory', 'arguments': {
                'action': 'replace', 'target': 'Use TSV', 'text': 'Use CSV'}})
            self.assertEqual(json.loads((root / 'memory.json').read_text()), ['Use CSV'])

    def test_exposure_requires_body_in_completed_wire_input_not_catalog_or_self_report(self):
        with tempfile.TemporaryDirectory() as folder:
            actor = Path(folder)
            version = {'name': 'sample', 'skill_id': 's:1', 'raw_sha256': 'hash', 'content': 'Full body\nsecond line'}
            execution = {'events': [
                {'type': 'tool', 'name': 'Skill', 'error': False, 'result': version},
                {'type': 'tool', 'name': 'notes_share', 'error': False}]}
            request = actor / 'wire/0001'
            atomic(request / 'request.json', {'input': [{'type': 'function_call_output',
                'output': json.dumps({'content': [{'type': 'text', 'text': json.dumps(version)}]})}]})
            self.assertFalse(codex_exposures(execution, [version], actor, ['notes_share'])[0]['body_proven'])
            atomic(request / 'result.json', {'status': 200})
            (request / 'response.raw').write_text('event: response.completed\ndata: {}\n')
            proof = codex_exposures(execution, [version], actor, ['notes_share'])[0]
            self.assertTrue(proof['body_proven'])
            self.assertEqual(proof['proof'][0]['subsequent_actions'], [1])
            atomic(request / 'request.json', {'input': [{'type': 'message', 'content': version['content']}]})
            self.assertFalse(codex_exposures(execution, [version], actor, ['notes_share'])[0]['body_proven'])

    def test_native_wire_usage_is_counted_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'episodes/task/actor/wire/0000'
            atomic(path / 'request.json', {'model': 'gpt-6.1-sol'})
            atomic(path / 'result.json', {'usage': {'prompt_tokens': 12, 'completion_tokens': 3}})
            actual = costs(root)['executor']
            self.assertEqual(actual['request_attempts'], 1)
            self.assertEqual(actual['input_tokens'], 12)
            self.assertEqual(actual['output_tokens'], 3)


if __name__ == '__main__':
    unittest.main()
