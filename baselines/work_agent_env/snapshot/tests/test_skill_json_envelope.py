import json
import unittest

from integrations.benchmark_tasks.contracts import digest
from integrations.skill_generator.transport import normalize_response


class SkillJSONEnvelopeTests(unittest.TestCase):
    def test_whole_fence_records_raw_identity_and_preserves_operation_content(self):
        content = '---\nname: Example\ndescription: Test.\n---\n# Workflow\nKeep these bytes.'
        calls = [{"name": "functions.new_skill_insert", "arguments": {"skill_name": "Example", "content": content}}]
        raw = '```json\n' + json.dumps(calls) + '\n```'
        normalized, metadata = normalize_response(raw)
        self.assertTrue(metadata['json_fence_removed'])
        self.assertEqual(metadata['raw_sha256'], digest(raw))
        self.assertEqual(json.loads(normalized)[0]['arguments']['content'], content)
        self.assertEqual(json.loads(normalized)[0]['name'], 'new_skill_insert')

    def test_explanatory_text_and_malformed_json_are_rejected(self):
        for raw in ('Explanation\n```json\n[]\n```', '```json\n[]\n```\nExplanation',
                    '```json\n[{bad}]\n```', '```python\n[]\n```'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_response(raw)
