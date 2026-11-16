import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skillrl.codex_mcp import serve
from skillrl.pg01_tools import CONTRACT, current_skill, edit_skill, tool_flags
from skillrl.storage import load
from skillrl.vendor.library import DeltaLibrary
from support import Provider, bundle, pipeline, proposal, seed


class PG01Tools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lib = seed(self.root, ['sum-check'])
        self.content = self.lib.model_view()[0]['content']
        entry = self.lib.state()['active']['sum-check']
        self.config = {'tools': [], 'skills': {'sum-check': {**entry, 'content': self.content}},
            'events': str(self.root/'events.jsonl'), 'skill_write': {'contract': CONTRACT,
            'task_id': 'PG01_UPDATE_001', 'library': str(self.lib.root), 'sequence': 'test'}}
        self.args = {'action': 'edit', 'name': 'sum-check', 'content': self.content+'\nCheck retained source scope.\n',
                     'expected_sha256': entry['raw_sha256'], 'reason': 'The public request requires retaining source scope.'}

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        value = current_skill(self.config, 'sum-check')
        Path(self.config['events']).write_text(json.dumps({'name':'Skill','arguments':{'name':'sum-check'},
            'result':value,'error':False})+'\n')

    def test_real_write_and_fresh_read_match_immutable_blob(self):
        self.read();result = edit_skill(self.config, self.args, 9)
        self.assertTrue(result['persisted']);self.assertEqual(result['source_actor'], 'executor')
        self.assertEqual(current_skill(self.config, 'sum-check')['content'], self.args['content'])
        self.assertEqual(load(self.lib.root/'state.json')['processed'][result['stage']], result['commit'])
        self.assertEqual((self.lib.root/'raw_versions'/(result['raw_sha256']+'.md')).read_text(), self.args['content'])

    def test_pg02_write_is_explicitly_scoped_and_has_real_receipts(self):
        from skillrl.pg01_tools import PG02_CONTRACT
        self.read();self.config['skill_write'].update(contract=PG02_CONTRACT,task_id='PG02_UPDATE_001')
        result=edit_skill(self.config,self.args,11)
        self.assertTrue(result['persisted']);self.assertEqual(result['source_actor'],'executor')
        self.assertEqual(current_skill(self.config,'sum-check')['content'],self.args['content'])

    def test_pg02_contract_cannot_enable_another_tasks_write(self):
        from skillrl.pg01_tools import PG02_CONTRACT
        self.read();self.config['skill_write'].update(contract=PG02_CONTRACT,task_id='PG01_UPDATE_001')
        with self.assertRaisesRegex(ValueError,'authorized'):edit_skill(self.config,self.args,11)

    def test_pg03_write_has_real_persisted_state_and_is_stage_scoped(self):
        from skillrl.pg01_tools import PG03_CONTRACT
        self.read();self.config['skill_write'].update(contract=PG03_CONTRACT,task_id='PG03_EVAL_NEAR_A_001')
        with self.assertRaisesRegex(ValueError,'authorized'):edit_skill(self.config,self.args,12)
        self.config['skill_write']['task_id']='PG03_UPDATE_001';result=edit_skill(self.config,self.args,12)
        self.assertTrue(result['persisted']);self.assertEqual(current_skill(self.config,'sum-check')['content'],self.args['content'])

    def test_pg03_read_support_preserves_baseline_memory_and_other_families(self):
        from skillrl.pg01_tools import PG03_CONTRACT,PG03_READ_STAGES
        flags={'skills_enabled':False,'memory_enabled':False,'session_search_enabled':True,'user_profile_enabled':False}
        for task in PG03_READ_STAGES:
            self.assertEqual(tool_flags(flags,task_id=task,contract=PG03_CONTRACT,persistence_allowed=True),flags if '_CONTROL_' in task else {**flags,'skills_enabled':True})
        for task in ['PG03_ATTEMPT_001','PG01_ATTEMPT_001','SM02_LEARN_A_001']:
            self.assertEqual(tool_flags(flags,task_id=task,contract=PG03_CONTRACT,persistence_allowed=True),flags)

    def test_pg04_edit_requires_the_update_stage_and_persists_actual_content(self):
        from skillrl.pg01_tools import PG04_CONTRACT
        self.read()
        self.config['skill_write'].update(contract=PG04_CONTRACT,task_id='PG04_EVAL_NEAR_A_001')
        before=(self.lib.root/'state.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'authorized'):
            edit_skill(self.config,self.args,14)
        self.assertEqual((self.lib.root/'state.json').read_bytes(),before)
        self.config['skill_write']['task_id']='PG04_UPDATE_001'
        result=edit_skill(self.config,self.args,14)
        self.assertTrue(result['persisted'])
        self.assertEqual(result['source_actor'],'executor')
        self.assertEqual(current_skill(self.config,'sum-check')['content'],self.args['content'])

    def test_pg04_does_not_change_existing_role_flags(self):
        from skillrl.pg01_tools import PG04_CONTRACT
        flags={'skills_enabled':True,'memory_enabled':True,'session_search_enabled':True,'user_profile_enabled':True}
        for task in ('PG04_ATTEMPT_001','PG04_UPDATE_001','PG04_EVAL_NEAR_A_001','PG01_ATTEMPT_001'):
            self.assertEqual(tool_flags(flags,task_id=task,contract=PG04_CONTRACT,persistence_allowed=True),flags)

    def test_pg05_only_restores_explicit_skill_reads_and_update_writes(self):
        from skillrl.pg01_tools import PG05_CONTRACT, PG05_READ_STAGES
        flags={'skills_enabled':False,'memory_enabled':False,'session_search_enabled':True,'user_profile_enabled':False}
        for task in PG05_READ_STAGES:
            self.assertEqual(tool_flags(flags,task_id=task,contract=PG05_CONTRACT,persistence_allowed=True),flags if '_CONTROL_' in task else {**flags,'skills_enabled':True})
        self.assertEqual(tool_flags(flags,task_id='PG05_ATTEMPT_001',contract=PG05_CONTRACT,persistence_allowed=True),flags)
        self.read();self.config['skill_write'].update(contract=PG05_CONTRACT,task_id='PG05_EVAL_NEAR_A_001')
        with self.assertRaisesRegex(ValueError,'authorized'):edit_skill(self.config,self.args,15)
        self.config['skill_write']['task_id']='PG05_UPDATE_001'
        result=edit_skill(self.config,self.args,15)
        self.assertTrue(result['persisted'])
        self.assertEqual(current_skill(self.config,'sum-check')['content'],self.args['content'])

    def test_pg06_real_editor_preserves_the_update_stage_boundary(self):
        from skillrl.pg01_tools import PG06_CONTRACT
        self.read();self.config['skill_write'].update(contract=PG06_CONTRACT,task_id='PG06_ATTEMPT_001')
        with self.assertRaisesRegex(ValueError,'authorized'):edit_skill(self.config,self.args,16)
        self.config['skill_write']['task_id']='PG06_UPDATE_001'
        result=edit_skill(self.config,self.args,16)
        self.assertTrue(result['persisted'])
        self.assertEqual(result['source_actor'],'executor')

    def test_all_pg_controls_keep_original_history_only_flags(self):
        from skillrl.pg01_tools import READ_STAGES, PG03_READ_STAGES, PG05_READ_STAGES, PG03_CONTRACT, PG05_CONTRACT
        original = dict(memory_enabled=False, user_profile_enabled=False, skills_enabled=False, session_search_enabled=True)
        for contract, tasks in ((CONTRACT, READ_STAGES), (PG03_CONTRACT, PG03_READ_STAGES), (PG05_CONTRACT, PG05_READ_STAGES)):
            for task in tasks:
                with self.subTest(contract=contract, task=task):
                    actual = tool_flags(original, task_id=task, contract=contract, persistence_allowed=True)
                    self.assertEqual(actual, original if '_CONTROL_' in task else {**original, 'skills_enabled': True})

    def test_write_without_full_read_is_rejected_and_changes_nothing(self):
        before=(self.lib.root/'state.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'full Skill read'):edit_skill(self.config,self.args,1)
        self.assertEqual((self.lib.root/'state.json').read_bytes(),before)

    def test_stale_version_is_rejected(self):
        self.read();edit_skill(self.config,self.args,1)
        with self.assertRaisesRegex(ValueError,'version changed'):edit_skill(self.config,self.args,2)

    def test_other_tasks_and_disabled_capability_cannot_write(self):
        self.read()
        for task in ('PG01_EVAL_NEAR_A_001','SM02_LEARN_A_001'):
            with self.subTest(task=task):
                self.config['skill_write']['task_id']=task
                with self.assertRaisesRegex(ValueError,'authorized'):edit_skill(self.config,self.args,1)
        self.config.pop('skill_write')
        with self.assertRaisesRegex(ValueError,'authorized'):edit_skill(self.config,self.args,1)

    def test_no_new_or_renamed_skill_can_be_smuggled_into_edit(self):
        self.read();self.args['content']=self.args['content'].replace('name: sum-check','name: another-check')
        with self.assertRaises(ValueError):edit_skill(self.config,self.args,1)
        self.assertEqual(set(self.lib.state()['active']),{'sum-check'})

    def test_mcp_catalog_and_calls_preserve_actual_receipts(self):
        self.read()
        requests=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
                  {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'skill_manage','arguments':self.args}},
                  {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'Skill','arguments':{'name':'sum-check'}}}]
        out=io.StringIO()
        with patch('sys.stdin',io.StringIO('\n'.join(json.dumps(r) for r in requests)+'\n')),patch('sys.stdout',out):
            serve(self.config)
        results=[json.loads(s)['result'] for s in out.getvalue().splitlines()]
        self.assertIn('skill_manage',[t['name'] for t in results[0]['tools']])
        self.assertTrue(json.loads(results[1]['content'][0]['text'])['persisted'])
        self.assertEqual(json.loads(results[2]['content'][0]['text'])['content'],self.args['content'])

    def test_flags_never_open_memory_history_or_other_family(self):
        flags={'skills_enabled':False,'memory_enabled':False,'session_search_enabled':True,'user_profile_enabled':False}
        changed=tool_flags(flags,task_id='PG01_EVAL_NEAR_A_001',contract=CONTRACT,persistence_allowed=True)
        self.assertEqual(changed,{**flags,'skills_enabled':True})
        for task,contract,persist in [('SM02_LEARN_A_001',CONTRACT,True),('PG01_ATTEMPT_001',CONTRACT,True),('PG01_EVAL_NEAR_A_001',None,True),('PG01_EVAL_NEAR_A_001',CONTRACT,False)]:
            self.assertEqual(tool_flags(flags,task_id=task,contract=contract,persistence_allowed=persist),flags)

    def test_curator_observes_post_actor_skill_not_stale_snapshot(self):
        with Provider([proposal('noop')]) as provider:
            (self.root/'case').mkdir()
            bench=bundle(self.root/'case');run=pipeline(self.root/'case',bench,provider)
            library=seed(run.root,['sum-check']);original=run.executor.execute
            def execute(*args):
                record=original(*args)
                body=library.model_view()[0]['content']+'\nA real actor edit.\n'
                library.apply('executor-unit-test',[{'name':'skill_update','arguments':{'skill_name':'sum-check','new_content':body}}])
                record['skill_write_capability']={'contract':CONTRACT}
                return record
            run.executor.execute=execute
            report=run.run();self.assertTrue(report['complete'])
            path=run.root/'episodes/task-1'
            self.assertNotIn('A real actor edit.',load(path/'snapshot.json')[0]['content'])
            self.assertIn('A real actor edit.',load(path/'curator-snapshot.json')[0]['content'])
            self.assertIn('A real actor edit.',(path/'curator/observation.json').read_text())
