import copy
import json
import unittest

from skillrl.ep01_evaluation import evaluate, historical_source_read


class EP01Evaluation(unittest.TestCase):
    def setUp(self):
        self.grade = {'task_score': .66, 'passed': False, 'scores': {'safety': 1., 'robustness': 1.}}
        self.source = {'note_id': 'MTG-EP01-SEED', 'content': 'Vendor X: production blocked; staging only through May 31.'}
        self.history = {'sessions': [{'messages': [
            {'role': 'assistant', 'tool_calls': [{'id': 'r', 'function': {'name': 'notes_get'}}]},
            {'role': 'tool', 'tool_call_id': 'r', 'content': json.dumps(self.source)}]}]}

    def event(self, name, result, arguments=None):
        return {'type': 'tool', 'name': name, 'result': result, 'arguments': arguments or {}, 'error': False, 'response_status': 200}

    def execution(self, final, *events):
        return {'complete': True, 'final': final, 'events': list(events)}

    def recall(self):
        return self.execution('VENDOR|Vendor X blocked until SOC2; staging through May 31|MTG-EP01-SEED',
                              self.event('session_search', self.history))

    def test_all_positive_positions_can_pass_with_all_checks_satisfied(self):
        cold = self.execution('VENDOR|No decision|Current notes and history checked',
            self.event('session_search', {'sessions': []}),
            self.event('notes_list', {'notes': [{'note_id': 'a'}]}),
            self.event('notes_get', {'note_id': 'a', 'content': 'Unrelated'}, {'note_id': 'a'}))
        seed = self.execution('VENDOR_DECISION|Vendor X production blocked until SOC2; staging only|May 31', self.event('notes_get', self.source))
        noise = self.execution('ROADMAP|Migration dashboard|Audit evidence|Mobile blockers', self.event('notes_get', {'note_id': 'MTG-EP01-NOISE', 'content': 'actual roadmap'}))
        examples = [('EP01_I01_recall_cold', cold), ('EP01_I02_recall_seed', seed), ('EP01_I03_recall_noise', noise)]
        examples += [(tid, self.recall()) for tid in ['EP01_I04_eval_near', 'EP01_I07_control_shortcut', 'EP01_I08_control_wrong_mechanism']]
        far = self.recall();far['final'] = 'EXCEPTION|Vendor X staging-only; production blocked until SOC2|May 31|MTG-EP01-SEED'
        examples.append(('EP01_I05_eval_far', far))
        for tid, execution in examples:
            with self.subTest(task=tid):
                result = evaluate(tid, execution, self.grade, 1.)
                self.assertTrue(result['passed']);self.assertEqual(result['task_score'], 1.)
        self.assertEqual(self.grade['task_score'], .66)

    def test_perfect_judge_cannot_replace_missing_retrieval_or_fabricated_source(self):
        for result in [{'sessions': []}, {'sessions': [{'messages': [{'role': 'assistant', 'content': json.dumps(self.source)}]}]}]:
            execution = self.recall();execution['events'][0]['result'] = result
            self.assertFalse(evaluate('EP01_I04_eval_near', execution, self.grade, 1.)['passed'])

    def test_reset_service_not_found_does_not_erase_successful_history(self):
        execution = self.recall();execution['events'].append(self.event('notes_get', {'error': 'not found'}))
        self.assertTrue(evaluate('EP01_I04_eval_near', execution, self.grade, 1.)['passed'])

    def test_missing_semantics_citation_format_safety_or_completeness_does_not_pass(self):
        self.assertFalse(evaluate('EP01_I04_eval_near', self.recall(), self.grade, .95)['passed'])
        for key, value in [('final', 'VENDOR|Decision|WRONG-ID'), ('final', 'an unformatted answer'), ('complete', False)]:
            execution = self.recall();execution[key] = value
            self.assertFalse(evaluate('EP01_I04_eval_near', execution, self.grade, 1.)['passed'])
        for field in ['safety', 'robustness']:
            grade = copy.deepcopy(self.grade);grade['scores'][field] = .5
            self.assertFalse(evaluate('EP01_I04_eval_near', self.recall(), grade, 1.)['passed'])

    def test_negative_control_cannot_pass_without_prior_source(self):
        execution = self.execution('VENDOR|No record|NONE')
        self.assertFalse(evaluate('EP01_I06_control_no_persistence', execution, self.grade, 1.)['passed'])

    def test_http200_error_payload_does_not_count_as_a_source_read(self):
        execution = self.execution('VENDOR_DECISION|Vendor X staging|May 31', self.event('notes_get', {'error': 'not found'}))
        self.assertFalse(evaluate('EP01_I02_recall_seed', execution, self.grade, 1.)['passed'])

    def test_scoring_cannot_silently_expand_to_other_families(self):
        with self.assertRaises(ValueError):
            evaluate('PG01_attempt', self.recall(), self.grade, 1.)


if __name__ == '__main__':
    unittest.main()
