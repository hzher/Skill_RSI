"""External-Skill capability, identity and idempotency boundaries without model calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

from integrations.model_endpoint import EndpointSettings
from integrations.skill_rsi_contract import (RSIError, checked_skill, descriptor_id, executor_descriptor,
                                             restrict_tools, skill_version, validate_request)
from integrations.skill_rsi_runtime import SkillRSI
from tests.test_model_task_executor import execution_root


def example_skill():
    return {'schema_version':'skill-v1','skill_id':'interface-fixture','objective':'Follow the public task',
            'steps':['Read the allowed source data','Write and verify the requested artifact'],
            'allowed_tools':['unit__read','write_workbook']}


def request(skill=None):
    return {'schema_version':'skill-rsi-evaluation-v1','request_id':str(uuid.uuid4()),'task_id':'unit','skill':skill}


class SkillRSITests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);execution_root(self.root)
        for name in ('integrations/skill_rsi_contract.py','integrations/skill_rsi_runtime.py','integrations/skill_rsi_locks.py'):
            (self.root/name).write_text('bound fixture')
        self.identity=patch('integrations.runtime_identity.expected_identity',return_value={'fixture':'environment'})
        self.identity.start();self.addCleanup(self.identity.stop)
        self.settings=EndpointSettings('openai_responses','https://unit.invalid','unit','unit-secret')
        self.calls=[]

    def runner(self, root, task, settings, **kwargs):
        self.calls.append(deepcopy(kwargs))
        skill=kwargs['skill']
        from integrations.model_task_executor import EXECUTION_SOURCES
        return {'run_id':uuid.uuid4().hex,'task_id':task,'requested_model':settings.model,'status':'passed',
                'evaluation_request_id':kwargs['evaluation_request_id'],
                'source_sha256':{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in (*EXECUTION_SOURCES,'integrations/skill_rsi_contract.py')},
                'environment_source_sha256':{'fixture':'environment'},
                'evaluation_scope':'skill_rsi','skill_sha256':skill_version(skill) if skill is not None else None,
                'measurement':{'reward':1,'mask_sample':False},'closed':True,'source_unchanged':True,
                'model_http_requests':2,'elapsed_seconds':.1,'model_reported_usage':{'input_tokens':10}}

    def service(self, runner=None, preflight=lambda:None):
        return SkillRSI(self.root,self.settings,runner=runner or self.runner,preflight=preflight)

    def test_skill_schema_and_bytes_are_validated_without_interpreting_steps(self):
        value=example_skill();original=deepcopy(value)
        result=checked_skill(value);result['steps'].append('extra')
        self.assertEqual(value,original)
        self.assertEqual(skill_version(value),skill_version(deepcopy(value)))
        for bad in ({**value,'scripts':['execute.py']},{**value,'steps':[]},{**value,'allowed_tools':['x','x']}):
            with self.subTest(bad=bad),self.assertRaises(RSIError):checked_skill(bad)

    def test_library_entry_imports_operator_dependencies_without_cli_search_path(self):
        actual_root=Path(__file__).resolve().parents[1]
        code="import sys;sys.path.insert(0,sys.argv[1]);from scripts.run_model_tasks import preflight;assert callable(preflight)"
        done=subprocess.run([sys.executable,'-I','-c',code,str(actual_root)],capture_output=True,timeout=20)
        self.assertEqual(done.returncode,0,done.stderr.decode(errors='replace'))

    def test_request_rejects_unknown_task_credential_or_path_fields_and_noncanonical_ids(self):
        good=request(example_skill())
        for bad in ({**good,'task_id':'other'},{**good,'api_key':'private'},
                    {**good,'skill_path':'../../private'},{**good,'request_id':'not-an-id'}):
            with self.subTest(bad=bad),self.assertRaises(RSIError):validate_request(bad,['unit'])
        self.assertEqual(validate_request(good,['unit']),good)

    def test_capability_intersection_never_grants_an_unlisted_tool(self):
        tools=[{'name':n,'parameters':{}} for n in ('unit__read','unit__write','verify_task','write_workbook')]
        mapping={t['name']:('mcp','unit',t['name']) for t in tools}
        allowed,routes=restrict_tools(tools,mapping,example_skill())
        self.assertEqual(set(routes),{'unit__read','write_workbook','verify_task'})
        self.assertEqual({t['name'] for t in allowed},set(routes))
        with self.assertRaisesRegex(RSIError,'not_available'):
            restrict_tools(tools,mapping,{**example_skill(),'allowed_tools':['unknown']})
        tools.append({'name':'write_workbook_from_results'});mapping['write_workbook_from_results']=('artifact',None,'write_workbook_from_results')
        with self.assertRaisesRegex(RSIError,'requires_workbook_permission'):
            restrict_tools(tools,mapping,{**example_skill(),'allowed_tools':['write_workbook_from_results']})

    def test_executor_fingerprint_changes_with_budget_and_source_but_contains_no_secret(self):
        first=executor_descriptor(self.root,self.settings,max_steps=10,wall_seconds=100)
        self.assertNotIn('unit-secret',json.dumps(first))
        changed=executor_descriptor(self.root,self.settings,max_steps=11,wall_seconds=100)
        self.assertNotEqual(descriptor_id(first),descriptor_id(changed))
        (self.root/'integrations/model_task_executor.py').write_text('changed')
        self.assertNotEqual(descriptor_id(first),descriptor_id(executor_descriptor(self.root,self.settings,max_steps=10,wall_seconds=100)))

    def test_exact_external_skill_is_consumed_and_same_request_never_bills_twice(self):
        service=self.service();body=request(example_skill())
        first=service.evaluate(body);second=self.service().evaluate(body)
        self.assertEqual(first,second);self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['skill'],body['skill'])
        self.assertEqual(first['state'],'completed');self.assertFalse(first['result']['training_eligible'])
        public=self.root/'evidence/skill_rsi/runs'/(body['request_id']+'.json')
        self.assertTrue(public.exists());self.assertNotIn(body['skill']['objective'],public.read_text())
        self.assertFalse((self.root/'.runtime/model-task-campaign.lock').exists())

    def test_reusing_request_id_with_changed_skill_is_a_conflict(self):
        service=self.service();body=request(example_skill());service.evaluate(body)
        body['skill']['steps'].append('changed')
        with self.assertRaisesRegex(RSIError,'request_id_conflict'):service.evaluate(body)
        self.assertEqual(len(self.calls),1)

    def test_unknown_execution_is_not_replayed_and_blocks_new_jobs(self):
        def failed(*args,**kwargs):self.calls.append(kwargs);raise RuntimeError('PRIVATE details')
        service=self.service(runner=failed);body=request(example_skill())
        first=service.evaluate(body);self.assertEqual(first['state'],'unknown')
        self.assertNotIn('PRIVATE',json.dumps(first))
        self.assertEqual(service.evaluate(body),first)
        later=service.evaluate(request(example_skill()))
        self.assertEqual(later['state'],'rejected');self.assertEqual(len(self.calls),1)
        self.assertEqual(later['result']['error'],'previous_evaluation_unresolved')

    def test_pending_process_intent_is_never_replayed_after_restarting_client(self):
        service=self.service();body=request(example_skill())
        service.store.reserve(body,service.executor_id)
        service.store.transition(body['request_id'],'accepted','running')
        self.assertEqual(self.service().evaluate(body)['state'],'running')
        self.assertEqual(self.calls,[])

    def test_orphan_accepted_intent_blocks_other_requests(self):
        service=self.service();body=request(example_skill())
        service.store.reserve(body,service.executor_id)
        result=service.evaluate(request())
        self.assertEqual(result['state'],'rejected');self.assertEqual(self.calls,[])

    def test_mismatched_result_cannot_claim_success(self):
        for changes in ({'evaluation_request_id':str(uuid.uuid4())},{'measurement':{'reward':0,'mask_sample':False}},
                        {'closed':'true'},{'run_id':'../../outside'},{'elapsed_seconds':float('inf')}):
            with self.subTest(changes=changes):
                def bad(*args,**kwargs):return {**self.runner(*args,**kwargs),**changes}
                # Isolate an unresolved result from the next scenario.
                service=self.service(runner=bad);body=request()
                result=service.evaluate(body)
                self.assertEqual(result['state'],'unknown')
                service.store.transition(body['request_id'],'unknown','rejected',{'test_cleanup':True})

    def test_preflight_rejection_has_no_model_calls_and_does_not_poison_later_requests(self):
        def busy():raise RuntimeError('Foreign environment details')
        service=self.service(preflight=busy)
        result=service.evaluate(request(example_skill()))
        self.assertEqual(result['state'],'rejected');self.assertEqual(self.calls,[])
        self.assertEqual(self.service().evaluate(request(example_skill()))['state'],'completed')

    def test_source_changes_during_execution_cannot_claim_a_completed_evaluation(self):
        def change(*args,**kwargs):
            report=self.runner(*args,**kwargs)
            (self.root/'integrations/model_task_executor.py').write_text('changed mid-run')
            return report
        result=self.service(runner=change).evaluate(request(example_skill()))
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(result['result']['error'],'executor_or_environment_changed')

    def test_baseline_and_skill_are_recorded_as_distinct_inputs(self):
        service=self.service()
        baseline=service.evaluate(request())['result'];skill=service.evaluate(request(example_skill()))['result']
        self.assertEqual(baseline['executor_id'],skill['executor_id'])
        self.assertIsNone(baseline['skill_sha256']);self.assertIsNotNone(skill['skill_sha256'])
        self.assertEqual(baseline['skill_mode'],'no_skill_baseline')


if __name__=='__main__':unittest.main()
