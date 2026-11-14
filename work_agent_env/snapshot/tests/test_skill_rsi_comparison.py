from copy import deepcopy
import unittest
import uuid

from integrations.skill_rsi_comparison import SkillComparison
from integrations.skill_rsi_contract import RSIError
from tests import test_skill_rsi as fixtures


class ComparisonTests(unittest.TestCase):
    setUp = fixtures.SkillRSITests.setUp
    runner = fixtures.SkillRSITests.runner
    service = fixtures.SkillRSITests.service
    def comparison(self):
        return {'schema_version':'skill-rsi-comparison-v1','request_id':str(uuid.uuid4()),
                'task_id':'unit','candidates':[None,fixtures.example_skill()]}

    def test_serial_baseline_and_skill_are_observations_without_automatic_winner(self):
        comparator=SkillComparison(self.service());body=self.comparison()
        first=comparator.evaluate(body)
        self.assertEqual(first['state'],'completed');self.assertEqual(len(self.calls),2)
        self.assertIsNone(self.calls[0]['skill']);self.assertEqual(self.calls[1]['skill'],fixtures.example_skill())
        self.assertFalse(first['result']['selection_eligible']);self.assertIsNone(first['result']['winner'])
        self.assertEqual(comparator.evaluate(body),first);self.assertEqual(len(self.calls),2)

    def test_unknown_first_arm_prevents_second_arm_and_is_not_resumed(self):
        def unknown(*args,**kwargs):return {**self.runner(*args,**kwargs),'status':'unknown','measurement':None}
        comparator=SkillComparison(self.service(runner=unknown));body=self.comparison()
        result=comparator.evaluate(body)
        self.assertEqual(result['state'],'unknown');self.assertEqual(len(self.calls),1)
        self.assertEqual(comparator.evaluate(body),result);self.assertEqual(len(self.calls),1)

    def test_changed_comparison_identity_conflicts_and_duplicate_candidates_are_rejected(self):
        comparator=SkillComparison(self.service());body=self.comparison();comparator.evaluate(body)
        changed=deepcopy(body);changed['candidates'][1]['objective']='different'
        with self.assertRaisesRegex(RSIError,'request_id_conflict'):comparator.evaluate(changed)
        changed=self.comparison();changed['candidates']=[None,None]
        with self.assertRaisesRegex(RSIError,'duplicate_comparison_candidate'):comparator.evaluate(changed)


if __name__=='__main__':unittest.main()
