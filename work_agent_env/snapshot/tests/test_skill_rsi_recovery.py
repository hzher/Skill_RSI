import hashlib
import json
import subprocess
import sys
import unittest
import uuid

from integrations.skill_rsi_contract import RSIError
from integrations.skill_rsi_recovery import reconcile
from tests import test_skill_rsi as fixtures


class RecoveryTests(unittest.TestCase):
    setUp = fixtures.SkillRSITests.setUp
    runner = fixtures.SkillRSITests.runner
    service = fixtures.SkillRSITests.service

    def retained(self, service, body):
        service.prepare(body);service.store.transition(body['request_id'],'accepted','running')
        report=self.runner(self.root,'unit',self.settings,skill=body['skill'],evaluation_request_id=body['request_id'])
        directory=self.root/'.runtime/model-task-runs'/report['run_id'];directory.mkdir(parents=True)
        report['resources_session_id']=str(uuid.uuid4())
        trace=directory/'trace.jsonl';trace.write_text(json.dumps({'kind':'close','value':{'resources_session_id':report['resources_session_id']}})+'\n')
        report['private_input_sha256']={trace.relative_to(self.root).as_posix():hashlib.sha256(trace.read_bytes()).hexdigest()}
        (directory/'manifest.json').write_text(json.dumps({'evaluation_request_id':body['request_id']}))
        (directory/'result.json').write_text(json.dumps(report))
        return directory

    def test_cancel_before_dispatch_never_calls_the_model(self):
        service=self.service();body=fixtures.request();service.prepare(body)
        result=reconcile(service,body['request_id'])
        self.assertEqual(result['state'],'rejected');self.assertEqual(self.calls,[])
        self.assertEqual(reconcile(service,body['request_id']),result)

    def test_real_process_exit_leaves_durable_intent_without_replay(self):
        body=fixtures.request()
        code='''import json,os,sys
from pathlib import Path
from unittest.mock import patch
from integrations.model_endpoint import EndpointSettings
from integrations.skill_rsi_runtime import SkillRSI
with patch('integrations.runtime_identity.expected_identity',return_value={'fixture':'environment'}):
    service=SkillRSI(Path(sys.argv[1]),EndpointSettings('openai_responses','https://unit.invalid','unit','unit-secret'),preflight=lambda:None)
    service.prepare(json.loads(sys.argv[2]))
    os._exit(23)
'''
        done=subprocess.run([sys.executable,'-c',code,str(self.root),json.dumps(body)],capture_output=True,timeout=20)
        self.assertEqual(done.returncode,23)
        service=self.service();self.assertEqual(service.evaluate(body)['state'],'accepted');self.assertEqual(self.calls,[])
        self.assertEqual(reconcile(service,body['request_id'])['state'],'rejected')

    def test_closed_retained_attempt_is_reconciled_without_reexecution(self):
        service=self.service();body=fixtures.request(fixtures.example_skill());self.retained(service,body)
        before=len(self.calls);result=reconcile(service,body['request_id'])
        self.assertEqual(result['state'],'completed');self.assertEqual(len(self.calls),before)
        self.assertEqual(service.evaluate(body),result)

    def test_tampered_or_missing_closure_cannot_clear_unknown(self):
        service=self.service();body=fixtures.request();directory=self.retained(service,body)
        (directory/'trace.jsonl').write_text('tampered')
        with self.assertRaisesRegex(RSIError,'recovery_input_hash_mismatch'):reconcile(service,body['request_id'])
        self.assertEqual(service.store.get(body['request_id'])['state'],'running')


if __name__=='__main__':unittest.main()
