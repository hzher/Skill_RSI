import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from skillrl.bounded_mcp import serve, session_view


class BoundedMCP(unittest.TestCase):
    def test_actual_reply_and_event_match_without_recursive_payload_or_source_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = {'id': 'earlier', 'messages': [{'role': 'assistant', 'content': 'x' * 1000000}]}
            source = {'id': 'approved', 'messages': [
                {'role': 'assistant', 'content': 'Original decision: staging only.'},
                {'role': 'assistant', 'tool_calls': [{'id': 'search', 'function': {'name': 'session_search'}}]},
                {'role': 'tool', 'tool_call_id': 'search', 'content': json.dumps({'sessions': [nested]})}]}
            raw = json.dumps([source]);(root / 'sessions.json').write_text(raw)
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                'name': 'session_search', 'arguments': {'query': '', 'session_id': 'approved'}}}
            output = io.StringIO()
            with patch('sys.stdin', io.StringIO(json.dumps(request) + '\n')), patch('sys.stdout', output):
                serve({'tools': [], 'sessions': str(root / 'sessions.json'), 'events': str(root / 'events.jsonl')})
            reply = json.loads(json.loads(output.getvalue())['result']['content'][0]['text'])
            event = json.loads((root / 'events.jsonl').read_text())
            self.assertEqual(event['result'], reply)
            self.assertEqual(reply['sessions'], [session_view(source)])
            self.assertIn('Original decision: staging only.', output.getvalue())
            self.assertLess(len(output.getvalue()), 10000)
            self.assertEqual((root / 'sessions.json').read_text(), raw)


if __name__ == '__main__':
    unittest.main()
