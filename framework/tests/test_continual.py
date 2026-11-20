import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skillrl.continual import native_context_events, notes_snapshot, session_arguments, task_prompt
from skillrl.context_hook import snapshot


class ContinualTests(unittest.TestCase):
    def config(self):
        return {'model': 'deepseek-v4-1-flash-261017', 'context_window': 200000}

    def test_resume_reuses_id_home_and_workspace(self):
        first = session_arguments(self.config(), 'container', '/workspace/.agent', 'http://api', 'http://rpc', 'session-id', False)
        second = session_arguments(self.config(), 'container', '/workspace/.agent', 'http://api', 'http://rpc', 'session-id', True)
        self.assertEqual(first[first.index('--session-id') + 1], second[second.index('--resume') + 1])
        self.assertIn('HOME=/workspace/.agent/home', second)
        self.assertEqual(second[second.index('-w') + 1], '/workspace')
        self.assertIn('BENCHMARK_RPC_URL=http://rpc', second)
        self.assertIn('CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000', second)
        self.assertNotIn('--fork-session', second)

    def test_notes_tools_are_optional_and_benchmark_actions_remain_available(self):
        arguments = session_arguments(self.config(), 'container', '/workspace/.agent', 'http://api', 'http://rpc', 'id', False)
        self.assertIn('Write', arguments[arguments.index('--tools') + 1])
        self.assertIn('mcp__benchmark__act', arguments[arguments.index('--allowedTools') + 1])
        prompt = task_prompt({'id': 'database_exploration-012'}, 2, 100)
        self.assertIn('task 3/100', prompt)
        self.assertIn('same persistent', prompt)
        self.assertIn('benchmark.act', prompt)

    def test_notes_and_context_snapshots_preserve_exact_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'notes').mkdir()
            (root / 'notes/facts.txt').write_text('retained fact\n')
            transcript = root / 'session.jsonl'
            transcript.write_bytes(b'{"type":"assistant"}\n')
            original = transcript.read_bytes()
            with patch.dict(os.environ, {'SKILLRL_NOTES_DIR': str(root / 'notes')}):
                record = snapshot({'hook_event_name': 'PreCompact', 'transcript_path': str(transcript), 'trigger': 'auto'}, root / 'events')
            saved = next((root / 'events').glob('*/transcript.jsonl'))
            self.assertEqual(saved.read_bytes(), original)
            self.assertEqual(transcript.read_bytes(), original)
            self.assertIn('facts.txt', record['notes'])
            manifest = notes_snapshot(root, root / 'copy')
            self.assertEqual((root / 'copy/facts.txt').read_text(), 'retained fact\n')
            self.assertEqual(manifest['facts.txt']['bytes'], 14)

    def test_context_events_are_structured_and_partial_lines_are_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'native.jsonl'
            path.write_text(json.dumps({'type': 'system', 'subtype': 'compact_boundary', 'compact_metadata': {'trigger': 'auto'}}) + '\n{"partial":')
            events = native_context_events(path)
            self.assertEqual(len(events), 1)
            self.assertIn('partial', path.read_text())


if __name__ == '__main__':
    unittest.main()
