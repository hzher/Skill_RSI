import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skillrl.codex_mcp import serve
from skillrl.pg01_tools import current_skill
from skillrl.skill_authoring import CONTRACT, save_skill
from support import seed


class PublicAuthoring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lib = seed(self.root, [])
        self.config = {'tools': [], 'skills': {}, 'events': str(self.root/'events.jsonl'),
                       'skill_write': {'contract': CONTRACT, 'task_id': 'LEARN',
                                       'authorized_task_ids': ['LEARN'], 'library': str(self.lib.root), 'sequence': 'test'}}
        self.args = {'action': 'create', 'name': 'scope-check', 'reason': 'The public learning task asks to save this procedure.',
                     'content': '---\nname: scope-check\ndescription: Check the scope of original records.\n---\nRead the public records and verify their scope.\n'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_persists_exact_model_body_and_is_readable(self):
        result = save_skill(self.config, self.args, 1)
        self.assertTrue(result['persisted'])
        self.assertEqual(result['source_actor'], 'executor')
        self.assertEqual(current_skill(self.config, 'scope-check')['content'], self.args['content'])
        self.assertEqual(result['commit']['applied_ops'][0]['op'], 'new_skill_insert')
        self.assertEqual((self.lib.root/'raw_versions'/(result['raw_sha256']+'.md')).read_text(), self.args['content'])

    def test_unauthorized_stage_and_duplicate_creation_cannot_write(self):
        self.config['skill_write']['task_id'] = 'CONTROL'
        with self.assertRaisesRegex(ValueError, 'authorized'):
            save_skill(self.config, self.args, 1)
        self.assertFalse(self.lib.state()['active'])
        self.config['skill_write']['task_id'] = 'LEARN'
        save_skill(self.config, self.args, 1)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            save_skill(self.config, self.args, 2)

    def test_edit_requires_real_full_read_and_current_version(self):
        result = save_skill(self.config, self.args, 1)
        edit = {**self.args, 'action': 'edit', 'expected_sha256': result['raw_sha256'],
                'content': self.args['content']+'Verify the decision.\n'}
        with self.assertRaisesRegex(ValueError, 'complete Skill read'):
            save_skill(self.config, edit, 2)
        (self.root/'events.jsonl').write_text(json.dumps({'name':'Skill','arguments':{'name':'scope-check'},
            'result':current_skill(self.config,'scope-check'),'error':False})+'\n')
        save_skill(self.config, edit, 2)
        with self.assertRaisesRegex(ValueError, 'version changed'):
            save_skill(self.config, edit, 3)

    def test_empty_library_advertises_real_create_and_read_tools(self):
        out = io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list'})+'\n')), patch('sys.stdout',out):
            serve(self.config)
        catalog = json.loads(out.getvalue())['result']['tools']
        self.assertEqual({v['name'] for v in catalog}, {'Skill','skill_manage'})
        manager = next(v for v in catalog if v['name']=='skill_manage')
        self.assertEqual(manager['inputSchema']['properties']['action']['enum'], ['create','edit'])
