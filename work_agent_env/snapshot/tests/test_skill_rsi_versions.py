import json
import unittest
import uuid

from integrations.skill_rsi_contract import RSIError, skill_version
from integrations.skill_rsi_versions import SkillVersions
from tests import test_skill_rsi as fixtures


class VersionTests(unittest.TestCase):
    setUp = fixtures.SkillRSITests.setUp

    def body(self, skill=None, expected=None):
        return {'schema_version':'skill-rsi-registration-v1','request_id':str(uuid.uuid4()),
                'skill':skill or fixtures.example_skill(),'expected_head':expected}

    def test_diagnostic_version_cas_and_retry_do_not_publish_a_generator_head(self):
        versions=SkillVersions(self.root);body=self.body();first=versions.register(body)
        self.assertEqual(versions.register(body),first)
        self.assertEqual(first['version'],skill_version(body['skill']))
        changed=fixtures.example_skill();changed['steps'].append('Check the result')
        with self.assertRaisesRegex(RSIError,'skill_version_conflict'):versions.register(self.body(changed))
        second=versions.register(self.body(changed,first['version']))
        self.assertFalse(versions.describe(first['version'])['is_current_head'])
        self.assertTrue(second['is_current_head'])
        self.assertIsNone(versions.registry.head('generator',changed['skill_id']))
        self.assertEqual(versions.load(second['version']),changed)
        self.assertNotIn(changed['objective'],json.dumps(second))

    def test_origin_identity_reuse_for_different_body_is_rejected(self):
        versions=SkillVersions(self.root);body=self.body();versions.register(body)
        body['skill']['objective']='Other objective'
        with self.assertRaisesRegex(RSIError,'skill_version_conflict'):versions.register(body)
        with self.assertRaises(RSIError):versions.load('../../outside')

    def test_current_head_supports_missing_and_cas_conflict_re_read(self):
        versions=SkillVersions(self.root);body=self.body();identity=body['skill']['skill_id']
        self.assertIsNone(versions.current_head(identity)['version'])
        first=versions.register(body)
        changed=fixtures.example_skill();changed['steps'].append('Recheck inputs')
        second=versions.register(self.body(changed,first['version']))
        with self.assertRaisesRegex(RSIError,'skill_version_conflict'):
            versions.register(self.body(changed,first['version']))
        head=versions.current_head(identity)
        self.assertEqual(head['version'],second['version'])
        self.assertEqual(set(head),{'schema_version','namespace','skill_id','version','training_eligible'})
        self.assertFalse(head['training_eligible'])
        self.assertIsNone(versions.registry.head('generator',identity))

    def test_head_lookup_preserves_exact_identity_and_does_not_interpret_sql(self):
        versions=SkillVersions(self.root);skill=fixtures.example_skill()
        skill['skill_id']=' 办公/报告 ?# '
        version=versions.register(self.body(skill))['version']
        self.assertEqual(versions.current_head(skill['skill_id'])['version'],version)
        self.assertIsNone(versions.current_head(skill['skill_id'].strip())['version'])
        self.assertIsNone(versions.current_head("' OR 1=1 --")['version'])
        for invalid in (None,1,[],{},'', ' ', 'x'*101, '\ud800'):
            with self.subTest(invalid=repr(invalid)),self.assertRaisesRegex(RSIError,'invalid_skill_identity'):
                versions.current_head(invalid)

    def test_current_head_rejects_missing_corrupted_and_cross_skill_targets(self):
        versions=SkillVersions(self.root);body=self.body();identity=body['skill']['skill_id']
        original=versions.register(body)['version']
        other=fixtures.example_skill();other['skill_id']='other'
        alternate=versions.register(self.body(other))['version']
        for target in ('bad-version','0'*64,alternate):
            with versions.registry.connect() as db:
                db.execute("UPDATE heads SET version=? WHERE namespace='diagnostic' AND skill_id=?",(target,identity))
            with self.subTest(target=target),self.assertRaisesRegex(RSIError,'skill_head_binding_invalid'):
                versions.current_head(identity)
        with versions.registry.connect() as db:
            db.execute("UPDATE heads SET version=? WHERE namespace='diagnostic' AND skill_id=?",(original,identity))
            db.execute("UPDATE versions SET body='{}' WHERE namespace='diagnostic' AND version=?",(original,))
        with self.assertRaisesRegex(RSIError,'skill_head_binding_invalid'):versions.current_head(identity)


if __name__=='__main__':unittest.main()
