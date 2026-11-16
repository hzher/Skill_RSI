import tempfile
from pathlib import Path
import unittest

import json
from skillrl.pastbench_full import RecordedJudge, judge_public_messages
from skillrl.storage import atomic


class CapturingJudge:
    def evaluate(self, *args):
        self.args = args
        return {'score': 1}


class PublicJudgeEvidence(unittest.TestCase):
    def judge(self, session=None):
        judge = RecordedJudge.__new__(RecordedJudge)
        judge.public_session = session
        judge.judge = CapturingJudge()
        return judge

    def test_summary_mode_preserves_original_judge_input(self):
        judge = self.judge()
        judge.evaluate('public task', 'answer', 'notes: 2 calls', 'private rubric')
        self.assertEqual(judge.judge.args, ('public task', 'answer', 'notes: 2 calls', 'private rubric'))

    def test_public_receipts_reach_judge_without_reading_neighboring_private_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            atomic(root / 'session.json', {'messages': [
                {'role': 'assistant', 'tool_calls': [{'id': 'read-1', 'function': {
                    'name': 'session_search', 'arguments': '{"query":"public project"}'}}]},
                {'role': 'tool', 'tool_call_id': 'read-1', 'content': 'original authorized decision record'}]})
            atomic(root / 'original-grade.json', {'secret': 'PRIVATE_ANSWER_SENTINEL'})
            judge = self.judge(root / 'session.json')
            judge.evaluate('public task', 'answer', 'notes: 2 calls', 'private rubric')
            actions = judge.judge.args[2]
            self.assertIn('session_search', actions)
            self.assertIn('original authorized decision record', actions)
            self.assertIn('read-1', actions)
            self.assertNotIn('PRIVATE_ANSWER_SENTINEL', actions)
            self.assertNotIn('private rubric', actions)

    def test_existing_internal_trace_is_not_duplicated_or_replaced(self):
        judge = self.judge(Path('/does-not-exist'))
        actions = 'summary\n## Agent internal tool trace\noriginal trace'
        judge.evaluate('task', 'answer', actions, 'rubric')
        self.assertEqual(judge.judge.args[2], actions)

    def test_background_curation_is_preserved_under_its_actual_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.json'
            executor = {'role': 'assistant', 'tool_calls': [{'id': 'executor-write',
                'function': {'name': 'helpdesk_update_ticket', 'arguments': '{"ticket_id":"A"}'}}]}
            curator = {'role': 'assistant', 'source_actor': 'framework_curator',
                'tool_calls': [{'id': 'curator-0', 'function': {'name': 'skill_manage',
                'arguments': '{"action":"create","source_actor":"framework_curator"}'}}]}
            atomic(path, {'messages': [executor, curator]})
            judge = self.judge(path)
            judge.evaluate('task', 'answer', 'service audit', 'original rubric')
            evidence = json.loads(judge.judge.args[2][judge.judge.args[2].index('{"executor_initial_context"'):])
            self.assertEqual(evidence['executor_messages'], [executor])
            self.assertEqual(evidence['framework_background_curation'], [curator])
            self.assertEqual(judge.judge.args[0], 'task')
            self.assertEqual(judge.judge.args[3], 'original rubric')

    def test_actual_injected_context_reaches_judge_without_a_memory_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.json'
            atomic(path, {'executor_public_context': {'initial_prompt':
                'Durable memory available to this session: authorized-current-fact'},
                'messages': [{'role': 'assistant', 'content': 'authorized-current-fact'}]})
            atomic(Path(tmp) / 'expectations.json', {'private': 'DO_NOT_EXPOSE_THIS_ANSWER'})
            judge = self.judge(path)
            judge.evaluate('task', 'answer', 'service audit', 'unchanged rubric')
            actions = judge.judge.args[2]
            self.assertIn('Durable memory available to this session', actions)
            self.assertNotIn('DO_NOT_EXPOSE_THIS_ANSWER', actions)
            self.assertNotIn('memory_read', actions)

    def test_history_view_preserves_decision_text_without_recursive_payload_expansion(self):
        deep = {'session_id': 'deep', 'messages': [{'role': 'assistant', 'content': 'x' * 1000000}]}
        prior = {'session_id': 'approved', 'messages': [
            {'role': 'user', 'content': 'Authoritative public decision: staging only.'},
            {'role': 'assistant', 'tool_calls': [{'id': 'old', 'function': {'name': 'session_search'}}]},
            {'role': 'tool', 'tool_call_id': 'old', 'content': json.dumps({'sessions': [deep]})}]}
        current = [
            {'role': 'assistant', 'tool_calls': [{'id': 'current', 'function': {'name': 'session_search'}}]},
            {'role': 'tool', 'tool_call_id': 'current', 'content': json.dumps({'sessions': [prior]})}]
        encoded = json.dumps(judge_public_messages(current))
        self.assertIn('Authoritative public decision: staging only.', encoded)
        self.assertIn('deep', encoded)
        self.assertIn('nested retrieval reference', encoded)
        self.assertLess(len(encoded), 5000)
        self.assertIn('x' * 100000, current[1]['content'])


if __name__ == '__main__':
    unittest.main()
