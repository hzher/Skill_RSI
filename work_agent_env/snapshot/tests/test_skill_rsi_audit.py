import hashlib
from copy import deepcopy
import json
from pathlib import Path
import unittest
import uuid

from integrations.skill_rsi_audit import audit_retained
from integrations.skill_rsi_comparison import SkillComparison
from integrations.model_task_executor import EXECUTION_SOURCES
from tests import test_skill_rsi as fixtures


class AuditTests(unittest.TestCase):
    setUp = fixtures.SkillRSITests.setUp
    runner = fixtures.SkillRSITests.runner
    service = fixtures.SkillRSITests.service

    def prepared(self):
        for name in ('integrations/skill_rsi_audit.py','scripts/audit_skill_rsi.py'):
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('source fixture')
        service=self.service(runner=self.retained_runner);self.descriptor=service.descriptor
        body=fixtures.request(fixtures.example_skill());service.evaluate(body)
        return service,body

    def retained_runner(self, root, task, settings, **kwargs):
        report=self.runner(root,task,settings,**kwargs)
        for key in ('protocol','max_steps','wall_seconds','max_output_tokens','reasoning_effort','endpoint_sha256'):
            report[key]=self.descriptor[key]
        directory=root/'.runtime/model-task-runs'/report['run_id'];directory.mkdir(parents=True)
        report['resources_session_id']=str(uuid.uuid4())
        for name in (*EXECUTION_SOURCES,'integrations/skill_rsi_contract.py'):
            target=directory/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((root/name).read_bytes())
        (directory/'manifest.json').write_text(json.dumps(report))
        trace=[]
        if kwargs['skill'] is not None:
            trace.append({'kind':'external_skill_input','value':{'skill':kwargs['skill'],'skill_sha256':report['skill_sha256']}})
        trace.append({'kind':'model_response','value':{'status':'completed','output':[]}})
        trace.append({'kind':'close','value':{'resources_session_id':report['resources_session_id']}})
        (directory/'trace.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in trace))
        report['private_input_sha256']={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file()}
        (directory/'result.json').write_text(json.dumps(report))
        return report

    def test_closed_inputs_and_comparison_can_be_rechecked_without_model_calls(self):
        service,body=self.prepared()
        comparison={'schema_version':'skill-rsi-comparison-v1','request_id':str(uuid.uuid4()),'task_id':'unit','candidates':[None,body['skill']]}
        SkillComparison(service).evaluate(comparison)
        before=len(self.calls);result=audit_retained(self.root)
        self.assertTrue(result['passed']);self.assertEqual(result['execution_receipts_checked'],3)
        self.assertEqual(len(self.calls),before);self.assertEqual(result['model_http_requests_started_by_audit'],0)
        self.assertNotIn(body['skill']['objective'],json.dumps(result));self.assertFalse(result['oracle_recomputed'])

    def test_changed_trace_fails_without_echoing_private_contents(self):
        service,body=self.prepared();run=service.store.get(body['request_id'])['result']['run_id']
        (self.root/'.runtime/model-task-runs'/run/'trace.jsonl').write_text('PRIVATE BUSINESS TEXT')
        result=audit_retained(self.root)
        self.assertFalse(result['passed']);self.assertNotIn('PRIVATE BUSINESS TEXT',json.dumps(result))
        self.assertEqual(result['runs'][0]['receipt_id'],body['request_id'])

    def test_parent_traversal_in_retained_hashes_is_rejected(self):
        service,body=self.prepared();run=service.store.get(body['request_id'])['result']['run_id']
        path=self.root/'.runtime/model-task-runs'/run/'result.json';report=json.loads(path.read_text())
        report['private_input_sha256']['.runtime/model-task-runs/'+run+'/../../outside']='0'*64
        path.write_text(json.dumps(report));result=audit_retained(self.root)
        self.assertFalse(result['passed']);self.assertEqual(result['runs'][0]['error'],'audit_input_reference_invalid')

    def test_changed_execution_source_is_not_hidden_by_rehashing_its_input_entry(self):
        service,body=self.prepared();run=service.store.get(body['request_id'])['result']['run_id']
        directory=self.root/'.runtime/model-task-runs'/run
        target=directory/'source/integrations/model_task_executor.py';target.write_text('changed source')
        path=directory/'result.json';report=json.loads(path.read_text())
        report['private_input_sha256'][target.relative_to(self.root).as_posix()]=hashlib.sha256(target.read_bytes()).hexdigest()
        path.write_text(json.dumps(report));result=audit_retained(self.root)
        self.assertFalse(result['passed']);self.assertFalse(result['runs'][0]['checks']['execution_source_matches_snapshot'])

    def prepared_comparison(self):
        service,body=self.prepared()
        request={'schema_version':'skill-rsi-comparison-v1','request_id':str(uuid.uuid4()),
                 'task_id':'unit','candidates':[None,body['skill']]}
        job=SkillComparison(service).evaluate(request)
        path=self.root/'evidence/skill_rsi/comparisons'/(request['request_id']+'.json')
        return service,request,job,path

    def rewrite_comparison(self,service,request,path,value,**columns):
        path.write_text(json.dumps(value))
        with service.store.connect() as database:
            database.execute('UPDATE comparisons SET result_json=? WHERE request_id=?',
                             (json.dumps(value),request['request_id']))
            for name,text in columns.items():
                self.assertIn(name,('request_json','digest','state'))
                database.execute('UPDATE comparisons SET '+name+'=? WHERE request_id=?',(text,request['request_id']))

    def test_reordered_candidate_feedback_is_rejected_even_when_saved_result_agrees(self):
        service,request,job,path=self.prepared_comparison()
        value=deepcopy(job['result']);value['arms'].reverse()
        self.rewrite_comparison(service,request,path,value)
        before=len(self.calls);result=audit_retained(self.root)
        self.assertFalse(result['passed']);self.assertFalse(result['comparisons'][0]['passed'])
        self.assertEqual(len(self.calls),before)

    def test_missing_private_comparison_record_is_not_accepted_from_public_receipts(self):
        service,request,job,path=self.prepared_comparison()
        with service.store.connect() as database:
            database.execute('DELETE FROM comparisons WHERE request_id=?',(request['request_id'],))
        result=audit_retained(self.root)
        self.assertFalse(result['passed'])
        self.assertEqual(result['comparisons'][0]['error'],'audit_comparison_record_unavailable')

    def test_candidate_request_and_comparison_source_are_content_addressed(self):
        service,request,job,path=self.prepared_comparison()
        changed=deepcopy(request);changed['candidates'].reverse()
        self.rewrite_comparison(service,request,path,job['result'],request_json=json.dumps(changed))
        self.assertFalse(audit_retained(self.root)['passed'])
        value=deepcopy(job['result']);value['comparison_source_sha256']='0'*64
        self.rewrite_comparison(service,request,path,value,request_json=json.dumps(request))
        self.assertFalse(audit_retained(self.root)['passed'])

    def test_public_and_persistent_comparison_result_must_agree(self):
        service,request,job,path=self.prepared_comparison()
        value=deepcopy(job['result']);value['interpretation']='PRIVATE_OMITTED_MISMATCH'
        path.write_text(json.dumps(value));result=audit_retained(self.root)
        self.assertFalse(result['passed']);self.assertNotIn('PRIVATE_OMITTED_MISMATCH',json.dumps(result))

    def test_complete_comparison_cannot_drop_an_arm_or_claim_other_counts(self):
        service,request,job,path=self.prepared_comparison()
        variants=[{'planned_arms':3},{'observed_arms':1},{'observed_arms':True},
                  {'arms':job['result']['arms'][:1],'observed_arms':1}]
        for changes in variants:
            with self.subTest(changes=list(changes)):
                value={**deepcopy(job['result']),**deepcopy(changes)}
                self.rewrite_comparison(service,request,path,value)
                self.assertFalse(audit_retained(self.root)['passed'])

    def test_feedback_from_another_comparison_cannot_be_spliced_in(self):
        service,request,job,path=self.prepared_comparison()
        other={**deepcopy(request),'request_id':str(uuid.uuid4())}
        replacement=SkillComparison(service).evaluate(other)
        value=deepcopy(job['result']);value['arms']=replacement['result']['arms']
        self.rewrite_comparison(service,request,path,value)
        result=audit_retained(self.root)
        self.assertFalse(result['passed']);self.assertEqual(len(result['comparisons']),2)

    def test_persistent_arm_state_and_input_cannot_disagree_with_saved_feedback(self):
        service,request,job,path=self.prepared_comparison()
        arm=job['result']['arms'][0]['request_id']
        with service.store.connect() as database:
            database.execute("UPDATE evaluations SET state='running' WHERE request_id=?",(arm,))
        self.assertFalse(audit_retained(self.root)['comparisons'][0]['passed'])
        with service.store.connect() as database:
            database.execute("UPDATE evaluations SET state='completed', request_json=? WHERE request_id=?",
                             (json.dumps(fixtures.request(fixtures.example_skill())),arm))
        self.assertFalse(audit_retained(self.root)['comparisons'][0]['passed'])

    def test_interrupted_comparison_and_selection_claims_are_never_certified(self):
        service,request,job,path=self.prepared_comparison()
        self.rewrite_comparison(service,request,path,job['result'],state='unknown')
        self.assertFalse(audit_retained(self.root)['comparisons'][0]['passed'])
        for changes in ({'sampling_deterministic':True},{'training_eligible':True},
                        {'initial_state_equality_attested':True},{'selection_eligible':True},
                        {'winner':0},{'error':'comparison_arm_unresolved'}):
            with self.subTest(changes=list(changes)):
                self.rewrite_comparison(service,request,path,{**job['result'],**changes},state='completed')
                self.assertFalse(audit_retained(self.root)['passed'])
        for field in ('winner','error'):
            with self.subTest(missing=field):
                value=deepcopy(job['result']);del value[field]
                self.rewrite_comparison(service,request,path,value,state='completed')
                self.assertFalse(audit_retained(self.root)['passed'])

    def test_invalid_or_oversized_private_comparison_json_has_fixed_feedback(self):
        service,request,job,path=self.prepared_comparison()
        for raw in ('PRIVATE_NOT_JSON','x'*(128*1024+1)):
            self.rewrite_comparison(service,request,path,job['result'],request_json=raw)
            result=audit_retained(self.root)
            self.assertFalse(result['passed']);self.assertNotIn('PRIVATE_NOT_JSON',json.dumps(result))

    def test_single_receipt_must_match_persistent_feedback(self):
        service,request=self.prepared()
        with service.store.connect() as database:
            database.execute('UPDATE evaluations SET result_json=? WHERE request_id=?',
                             (json.dumps({'error':'PRIVATE_CORRUPT_FEEDBACK'}),request['request_id']))
        result=audit_retained(self.root)
        self.assertFalse(result['passed']);self.assertNotIn('PRIVATE_CORRUPT_FEEDBACK',json.dumps(result))

    def test_single_receipt_trace_and_diagnostic_qualification_cannot_be_rewritten(self):
        service,request=self.prepared()
        path=self.root/'evidence/skill_rsi/runs'/(request['request_id']+'.json')
        original=json.loads(path.read_text())
        variants=[{'trace_ref':'model-task:'+uuid.uuid4().hex},{'skill_mode':'no_skill_baseline'},
                  {'training_eligible':True},{'training_updates':False},{'training_updates':1},
                  {'official_score_available':True},{'initial_state_equality_attested':True},
                  {'sampling_deterministic':True},{'schema_version':'unrecognized'}]
        for changes in variants:
            with self.subTest(changes=list(changes)):
                value={**original,**changes};path.write_text(json.dumps(value))
                with service.store.connect() as database:
                    database.execute('UPDATE evaluations SET result_json=? WHERE request_id=?',
                                     (json.dumps(value),request['request_id']))
                self.assertFalse(audit_retained(self.root)['passed'])


if __name__=='__main__':unittest.main()
