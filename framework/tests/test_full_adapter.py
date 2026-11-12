import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from skillrl.codex_mcp import serve
from skillrl.storage import atomic, load
from support import Provider, bundle, pipeline, proposal


class FullContracts(unittest.TestCase):
    def test_authorized_session_search_returns_original_text_and_keeps_other_family_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            atomic(root / 'sessions.json', [{'id': 'approved', 'messages': [{'role': 'assistant', 'content': 'Release Orion only in staging.'}]},
                                           {'id': 'unrelated', 'messages': [{'role': 'assistant', 'content': 'Finance review'}]}])
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': 'session_search', 'arguments': {'query': 'Orion', 'limit': 1}}}
            out = io.StringIO()
            with patch('sys.stdin', io.StringIO(json.dumps(request) + '\n')), patch('sys.stdout', out):
                serve({'tools': [], 'sessions': str(root / 'sessions.json'), 'events': str(root / 'events.jsonl')})
            response = json.loads(out.getvalue())['result']
            result = json.loads(response['content'][0]['text'])
            self.assertEqual(result['sessions'][0]['id'], 'approved')
            self.assertEqual(result['sessions'][0]['messages'][0]['content'], 'Release Orion only in staging.')
            self.assertEqual(len(result['sessions']), 1)
            self.assertEqual(json.loads((root / 'events.jsonl').read_text())['name'], 'session_search')

    def test_score_after_curator_observes_real_commit_without_exposing_score_to_curator(self):
        with tempfile.TemporaryDirectory() as tmp, Provider([proposal()]) as provider:
            bench = bundle(tmp)
            run = pipeline(tmp, bench, provider)
            bench.evaluate_after_curation = True
            original = bench.evaluate
            observed = []
            def evaluate(task, workspace, execution):
                commit = load(run.root / 'episodes/task-1/curator/commit.json')
                observed.append(bool(commit['applied_ops']))
                return original(task, workspace, execution)
            bench.evaluate = evaluate
            report = run.run()
            self.assertTrue(report['complete'])
            self.assertEqual(observed, [True])
            self.assertFalse(report['score_visibility_to_curator'])
            observation = (run.root / 'episodes/task-1/curator/observation.json').read_text()
            self.assertNotIn('"score"', observation)


if __name__ == '__main__':
    unittest.main()
