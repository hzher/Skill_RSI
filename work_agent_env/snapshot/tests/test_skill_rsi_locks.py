import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

from integrations.skill_rsi_contract import RSIError
from integrations.skill_rsi_locks import recover_lock,rsi_lock
from integrations.skill_rsi_recovery import reconcile
from tests import test_skill_rsi as fixtures
from tests import test_skill_rsi_recovery as recovery_fixtures
from workenv.leases import owned_lock


class LockRecoveryTests(unittest.TestCase):
    setUp=fixtures.SkillRSITests.setUp
    service=fixtures.SkillRSITests.service
    runner=fixtures.SkillRSITests.runner
    retained=recovery_fixtures.RecoveryTests.retained

    def dead_owner(self,body,which='campaign',running=False):
        code='''import json,os,sys
from pathlib import Path
from unittest.mock import patch
from integrations.model_endpoint import EndpointSettings
from integrations.skill_rsi_runtime import SkillRSI
from integrations.skill_rsi_locks import rsi_lock
with patch('integrations.runtime_identity.expected_identity',return_value={'fixture':'environment'}):
    service=SkillRSI(Path(sys.argv[1]),EndpointSettings('openai_responses','https://unit.invalid','unit','unit-secret'),preflight=lambda:None)
    body=json.loads(sys.argv[2]);which=sys.argv[3]
    if which=='campaign':service.prepare(body)
    with rsi_lock(service,which,'skill_rsi_evaluation' if which=='campaign' else 'skill_rsi_http',request_id=body['request_id'] if which=='campaign' else None):
        if sys.argv[4]=='running':service.store.transition(body['request_id'],'accepted','running')
        os._exit(23)
'''
        done=subprocess.run([sys.executable,'-c',code,str(self.root),json.dumps(body),which,'running' if running else 'accepted'],capture_output=True,timeout=20)
        self.assertEqual(done.returncode,23,done.stderr.decode(errors='replace'))
        path=self.root/('.runtime/model-task-campaign.lock' if which=='campaign' else '.runtime/skill-rsi-http.lock')
        return path,json.loads(path.read_text())

    def test_real_dead_campaign_owner_is_recovered_then_undispatched_request_can_be_cancelled(self):
        service=self.service();body=fixtures.request();path,_=self.dead_owner(body)
        result=recover_lock(service,'campaign')
        self.assertEqual(result['state'],'recovered');self.assertFalse(path.exists())
        self.assertEqual(service.store.get(body['request_id'])['state'],'accepted')
        self.assertFalse(result['evaluation_state_modified']);self.assertFalse(result['task_closure_proven_by_lock_recovery'])
        self.assertEqual(reconcile(service,body['request_id'])['state'],'rejected');self.assertEqual(self.calls,[])
        self.assertEqual(recover_lock(service,'campaign')['state'],'already_absent')
        self.assertNotIn('host_sha256',result);self.assertNotIn('process',result)

    def test_running_request_is_preserved_and_never_replayed(self):
        service=self.service();body=fixtures.request();self.dead_owner(body,running=True)
        recover_lock(service,'campaign')
        self.assertEqual(service.evaluate(body)['state'],'running');self.assertEqual(self.calls,[])
        self.assertEqual(service.evaluate(fixtures.request())['state'],'rejected')

    def test_retained_closed_result_can_be_reconciled_after_dead_owner_recovery(self):
        service=self.service();body=fixtures.request();self.retained(service,body)
        self.dead_owner(body);before=len(self.calls);recover_lock(service,'campaign')
        self.assertEqual(reconcile(service,body['request_id'])['state'],'completed')
        self.assertEqual(len(self.calls),before)

    def test_dead_http_owner_is_recovered_without_creating_or_modifying_a_job(self):
        service=self.service();path,_=self.dead_owner(fixtures.request(),which='http')
        self.assertEqual(recover_lock(service,'http')['state'],'recovered');self.assertFalse(path.exists())
        self.assertEqual(self.calls,[])
        with service.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM evaluations').fetchone()[0],0)

    def test_live_owner_and_unknown_process_identity_are_preserved(self):
        service=self.service();body=fixtures.request();service.prepare(body)
        with rsi_lock(service,'campaign','skill_rsi_evaluation',request_id=body['request_id']) as record:
            path=self.root/'.runtime/model-task-campaign.lock';raw=path.read_bytes()
            with self.assertRaisesRegex(RSIError,'still_alive'):recover_lock(service,'campaign')
            unknown={'pid':record['process']['pid'],'source':record['process']['source'],'status':'unknown','creation_id':None}
            with patch('integrations.skill_rsi_locks.process_identity',return_value=unknown):
                with self.assertRaisesRegex(RSIError,'identity_unavailable'):recover_lock(service,'campaign')
            self.assertEqual(path.read_bytes(),raw)

    def test_foreign_task_lock_and_unattested_legacy_lock_are_preserved(self):
        service=self.service();path=self.root/'.runtime/model-task-campaign.lock'
        for kind,code in (('nemo_real_model_tasks','not_owned'),('skill_rsi_evaluation','record_unavailable')):
            with owned_lock(path,self.root,kind):
                raw=path.read_bytes()
                with self.assertRaisesRegex(RSIError,code):recover_lock(service,'campaign')
                self.assertEqual(path.read_bytes(),raw)

    def test_changed_host_attestation_is_rejected(self):
        service=self.service();body=fixtures.request();path,record=self.dead_owner(body)
        claim=service.store.directory/'lock-claims'/(record['owner_token']+'.json')
        value=json.loads(claim.read_text());value['host_sha256']='0'*64;claim.write_text(json.dumps(value))
        raw=path.read_bytes()
        with self.assertRaisesRegex(RSIError,'host_identity_changed'):recover_lock(service,'campaign')
        self.assertEqual(path.read_bytes(),raw)

    def test_reused_pid_does_not_cause_any_process_to_be_killed(self):
        service=self.service();path,record=self.dead_owner(fixtures.request())
        actual={**record['process'],'status':'alive','creation_id':'a-different-process-creation'}
        with patch('integrations.skill_rsi_locks.process_identity',return_value=actual):
            result=recover_lock(service,'campaign')
        self.assertEqual(result['owner_observation'],'pid_reused');self.assertFalse(path.exists())

    def test_replacement_during_preflight_is_preserved(self):
        service=self.service();path,record=self.dead_owner(fixtures.request())
        replacement={**record,'owner_token':uuid.uuid4().hex}
        def replace():path.write_text(json.dumps(replacement))
        service.preflight=replace
        with self.assertRaisesRegex(RSIError,'ownership_changed'):recover_lock(service,'campaign')
        self.assertEqual(json.loads(path.read_text()),replacement)

    def test_runtime_preflight_failure_leaves_original_lock_and_hides_details(self):
        service=self.service();path,_=self.dead_owner(fixtures.request());raw=path.read_bytes()
        def unavailable():raise RuntimeError('PRIVATE runtime detail')
        service.preflight=unavailable
        with self.assertRaisesRegex(RSIError,'lock_runtime_not_ready') as caught:recover_lock(service,'campaign')
        self.assertNotIn('PRIVATE',str(caught.exception));self.assertEqual(path.read_bytes(),raw)


if __name__=='__main__':unittest.main()
