"""Behavioral checks for independent office tasks, provenance and file capabilities."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workenv import core,taskpacks,file_adapter
from workenv.file_tools import FileTools
from workenv.office_logic import reference,verify
from workenv.runner import execute,evaluate_run,reset_run


class LocalContracts(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name).resolve()
        self.patches=[patch.object(module,'ROOT',self.root) for module in (core,taskpacks,file_adapter)]
        for p in self.patches:p.start()
        (self.root/'tasks').mkdir()
        core.save(self.root/'tasks/registry.json',{'contract_version':'0.2','tasks':[]})

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.directory.cleanup()

    def add(self,family='receivables',variant='base',split='dev'):
        return taskpacks.write_pack(taskpacks.pack(family,11,variant,split))['task_id']

    def run_task(self,task,mode='known_correct'):
        return asyncio.run(execute(mode,task_id=task))

    def test_both_task_families_complete_and_independent_reverify(self):
        for family in ('receivables','meeting'):
            with self.subTest(family=family):
                result=self.run_task(self.add(family))
                self.assertIs(result['task_passed'],True)
                self.assertIs(evaluate_run(result['run_id'])['task_passed'],True)

    def test_interleaving_and_reset_do_not_change_other_task(self):
        first=self.run_task(self.add())
        second=self.run_task(self.add('meeting'))
        other=core.run_directory(second['run_id'])
        before=core.file_snapshot(other/'workspace')
        self.assertIs(evaluate_run(first['run_id'])['task_passed'],True)
        reset=reset_run(first['run_id'])
        self.assertTrue(reset['matches_initial'])
        self.assertTrue(reset['archived_artifacts_preserved'])
        self.assertEqual(before,core.file_snapshot(other/'workspace'))

    def test_changed_requirements_reject_previous_answers(self):
        for family in ('receivables','meeting'):
            task=self.add(family,'changed_constraint')
            self.assertIs(self.run_task(task)['task_passed'],True)
            self.assertIs(self.run_task(task,'old_answer')['task_passed'],False)

    def test_scheduling_accepts_a_different_feasible_solution(self):
        task=self.add('meeting')
        a=self.run_task(task)
        b=self.run_task(task,'alternative')
        self.assertIs(b['task_passed'],True)
        self.assertNotEqual((core.run_directory(a['run_id'])/'workspace/output/schedule.csv').read_bytes(),
                            (core.run_directory(b['run_id'])/'workspace/output/schedule.csv').read_bytes())

    def test_bad_artifacts_and_false_claims_rejected(self):
        task=self.add()
        for mode in ('empty','claim_only','wrong_object','partial','wrong_amount','missing_artifact','corrupt_artifact'):
            with self.subTest(mode=mode):
                result=self.run_task(task,mode)
                self.assertEqual(result['evaluation_status'],'completed')
                self.assertIs(result['task_passed'],False)

    def test_changing_visible_input_does_not_change_verifier_truth(self):
        result=self.run_task(self.add(),'tamper_input')
        self.assertIs(result['task_passed'],False)
        run=core.run_directory(result['run_id'])
        self.assertNotEqual((run/'private/inputs/policy.json').read_bytes(),(run/'workspace/inputs/policy.json').read_bytes())

    def test_verifier_failure_remains_unknown(self):
        result=self.run_task(self.add(),'verifier_error')
        self.assertEqual(result['evaluation_status'],'error')
        for name in ('task_passed','score','reward'):self.assertIsNone(result[name])

    def test_capability_rejects_private_parent_absolute_and_input_writes(self):
        result=self.run_task(self.add())
        workspace=core.run_directory(result['run_id'])/'workspace'
        tools=FileTools(workspace,[])
        for path in ('../private/inputs/policy.json','C:\\other\\secret','/etc/passwd','output/a:stream','inputs/.hidden'):
            with self.subTest(path=path),self.assertRaises((ValueError,OSError)):
                tools.call('read_file',{'path':path})
        before=(workspace/'inputs/policy.json').read_bytes()
        with self.assertRaises(ValueError):tools.call('write_file',{'path':'inputs/policy.json','content':'{}'})
        self.assertEqual(before,(workspace/'inputs/policy.json').read_bytes())

    def test_tool_size_budget(self):
        result=self.run_task(self.add())
        tools=FileTools(core.run_directory(result['run_id'])/'workspace',[])
        with self.assertRaises(ValueError):tools.call('write_file',{'path':'output/too_big.txt','content':'x'*(512*1024+1)})

    def test_immutable_pack_and_world_split_boundaries(self):
        self.add()
        self.assertEqual(taskpacks.pack('receivables',11),taskpacks.pack('receivables',11))
        self.assertEqual(taskpacks.audit_registry()['local_file_tasks'],1)
        with self.assertRaises(ValueError):self.add(variant='changed_constraint',split='test')
        spec=core.load_task('local-receivables-11-base')
        p=self.root/spec['source_path']/'inputs/invoices.csv'
        p.write_text('tampered',encoding='utf-8')
        with self.assertRaises(ValueError):taskpacks.audit_registry()

    def test_replay_protocol_finish_is_independently_verified(self):
        task=self.add()
        good=self.run_task(task,'replay_success')
        self.assertIs(good['task_passed'],True)
        self.assertEqual(good['execution_kind'],'replay_test_double')
        self.assertIsNone(good['model_id'])
        for mode in ('replay_no_write','replay_bad_path','replay_budget','replay_timeout'):
            result=self.run_task(task,mode)
            self.assertIs(result['task_passed'],False)
        recovered=self.run_task(task,'replay_recover')
        self.assertIs(recovered['task_passed'],True)
        record=core.read_json(core.run_directory(recovered['run_id'])/'policy_loop.json')
        self.assertEqual(record['tool_errors'],1)
        self.assertEqual(record['real_model_calls'],0)


if __name__=='__main__':
    unittest.main()
