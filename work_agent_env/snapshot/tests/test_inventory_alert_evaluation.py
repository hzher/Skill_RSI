"""Verifier tests preserve unknowns and never convert component results to reward."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from adapters import inventory_alert_evaluation as evaluation
from adapters import inventory_alert_artifacts as artifacts
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile
from tests.test_inventory_alert_validation import fixture
from tests.test_inventory_data import snapshot_fixture,response
from workenv.core import ResourceError,save


def output(passed=True):
    return '\n'.join(['=== Checking out_of_stock_report.txt ===','=== Checking Google Calendar ===',
        '=== Checking Emails ===','[PASS] fixture' if passed else '[FAIL] fixture','=== SUMMARY ===',
        '  Passed: '+('1' if passed else '0'),'  Failed: '+('0' if passed else '1'),
        '  Overall: '+('PASS' if passed else 'FAIL')])+'\n'


class InventoryOriginalParserTests(unittest.TestCase):
    def test_valid_original_pass_fail_and_windows_newlines(self):
        self.assertIs(evaluation.classify_original(0,output(),''),True)
        self.assertIs(evaluation.classify_original(1,output(False),''),False)
        self.assertIs(evaluation.classify_original(0,output().replace('\n','\r\n'),''),True)

    def test_inconsistent_summary_missing_repeated_markers_stderr_and_code_are_unknown(self):
        for code,stdout,stderr in ((True,output(),''),(2,output(),''),(0,output(),'Traceback'),
            (0,output().replace('Passed: 1','Passed: 2'),''),(0,output().replace('=== Checking Emails ===',''),''),
            (0,output()+'=== SUMMARY ===',''),(0,output().replace('[PASS]','[FAIL]'),''),
            (1,output(),''),(0,'x'*(256*1024+1),'')):
            self.assertIsNone(evaluation.classify_original(code,stdout,stderr))

    def test_original_fail_with_passed_failed_count_contradiction_is_unknown(self):
        self.assertIsNone(evaluation.classify_original(0,output(False).replace('Overall: FAIL','Overall: PASS'),''))

    def test_component_combination_never_assigns_task_reward_or_training(self):
        for original,supplemental,expected in ((True,True,True),(False,True,False),(True,False,False),(None,True,None)):
            result=evaluation.combine(original,{'supplemental_passed':supplemental,'evaluation_status':'completed'})
            self.assertIs(result['component_measurement_passed'],expected)
            self.assertIsNone(result['task_passed']);self.assertIsNone(result['reward'])
        self.assertIsNone(evaluation.combine(True,{'supplemental_passed':False,'evaluation_status':'error'})['component_measurement_passed'])

    def test_reference_text_comes_from_independent_sql_and_rejects_incompatible_truth(self):
        state=snapshot_fixture()
        self.assertEqual(evaluation.sql_report(state),'7 - Mug -  - Uncategorized\n42 - Tea - Large - T42 - Food')
        state['sql_zero_stock'][0]['name']='changed'
        with self.assertRaises(ResourceError):evaluation.sql_report(state)


class InventoryEvaluatorOwnershipTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.profile=EpisodeProfile(self.root,'f1895a7d-8626-4818-839a-d5ddab57b4a0',0)
        self.workspace=self.profile.base/'inventory-workspace';self.workspace.mkdir(parents=True)
        save(self.profile.base/'cluster_owner.json',self.profile.record())
        save(self.profile.base/'inventory-initialized.json',{'state':'ready','task_id':evaluation.data.TASK_ID,
            'profile':self.profile.record(),'contract_source':evaluation.validation.source_identity()})
        self.cluster=EpisodeCluster(self.profile);self.cluster.started=True
        self.state=snapshot_fixture();values=fixture()
        self.state['events']=values[2];self.state['email']['messages']=values[3];self.state['email']['sent_log']=values[4]
        artifacts.write_report(self.root,self.cluster,values[1])
        self.pages=self.profile.base/'pages.json';save(self.pages,[{'name':'woo_products_list','arguments':{'page':1,'perPage':10},'response':response(self.state['products'])}])
        self.proof={'schema_version':'inventory-evaluation-source-v1','task_id':evaluation.data.TASK_ID,
            'profile':self.profile.record(),'contract_source':evaluation.validation.source_identity(),
            'readonly_sha256':self.state['readonly_sha256'],'pages_ref':self.pages.relative_to(self.root).as_posix(),
            'pages_sha256':hashlib.sha256(self.pages.read_bytes()).hexdigest()}
        save(self.profile.base/evaluation.SOURCE_NAME,self.proof)

    def run_evaluator(self,before=None,after=None,side_effect=None):
        snapshots=[before or self.state,after or self.state]
        returned=SimpleNamespace(returncode=0,stdout=output().encode(),stderr=b'')
        with patch.object(evaluation.data,'snapshot',side_effect=snapshots),patch.object(evaluation,'constant_ids',return_value=[7,42]),\
            patch.object(evaluation.data,'public_blob',return_value=(b'# fixture original evaluator',{'sha256':'a'*64})),\
            patch.object(evaluation.subprocess,'run',side_effect=side_effect,return_value=returned):
            return evaluation.evaluate(self.root,self.cluster)

    def test_valid_owned_fixture_measures_component_only_and_writes_sql_reference(self):
        result=self.run_evaluator()
        self.assertIs(result['component_measurement_passed'],True)
        for key in ('nemo_task_executed','runtime_activation','training_eligible','upstream_reference_used','official_benchmark_score_available'):
            self.assertIs(result[key],False)
        self.assertEqual(result['reference_mode'],'project_derived_sql_oracle')
        self.assertIsNone(result['reward']);self.assertIsNone(result['task_passed'])
        reference=self.root/Path(result['evaluation_ref']).parent/'oracle/out_of_stock_report.txt'
        self.assertEqual(reference.read_text(),'7 - Mug -  - Uncategorized\n42 - Tea - Large - T42 - Food')

    def test_wrong_business_output_is_false_even_when_original_parser_passes(self):
        artifacts.write_report(self.root,self.cluster,'Wrong report contents')
        result=self.run_evaluator()
        self.assertIs(result['original_passed'],True)
        self.assertIs(result['component_measurement_passed'],False)
        self.assertIsNone(result['task_passed'])

    def test_missing_or_rebound_proof_is_unknown_before_original_execution(self):
        for key,value in (('profile',{}),('task_id','other'),('contract_source',{}),('readonly_sha256','wrong'),('pages_sha256','wrong')):
            save(self.profile.base/evaluation.SOURCE_NAME,{**self.proof,key:value})
            result=self.run_evaluator();self.assertIsNone(result['component_measurement_passed'])
            self.assertFalse(result['original_evaluator_executed'])
        (self.profile.base/evaluation.SOURCE_NAME).unlink()
        self.assertIsNone(self.run_evaluator()['component_measurement_passed'])

    def test_proof_cannot_reference_a_foreign_workspace_even_with_correct_sha(self):
        other=self.root/'foreign-pages.json';other.write_bytes(self.pages.read_bytes())
        save(self.profile.base/evaluation.SOURCE_NAME,{**self.proof,'pages_ref':other.relative_to(self.root).as_posix()})
        result=self.run_evaluator()
        self.assertIsNone(result['component_measurement_passed']);self.assertFalse(result['original_evaluator_executed'])

    def test_source_snapshot_change_after_original_run_remains_unknown(self):
        changed=deepcopy(self.state);changed['events']=[]
        result=self.run_evaluator(after=changed)
        self.assertTrue(result['original_evaluator_executed']);self.assertIsNone(result['original_passed'])
        self.assertIsNone(result['component_measurement_passed'])

    def test_pending_text_intent_blocks_evaluation_and_preserves_it(self):
        path=self.profile.base/(artifacts.PREFIX+'0'*32+'.json');save(path,{'state':'prepared'})
        before=path.read_bytes();result=self.run_evaluator()
        self.assertFalse(result['original_evaluator_executed']);self.assertIsNone(result['component_measurement_passed'])
        self.assertEqual(path.read_bytes(),before)

    def test_stopped_and_foreign_resources_cannot_acquire_completed_results(self):
        self.cluster.started=False
        result=evaluation.evaluate(self.root,self.cluster)
        self.assertEqual(result['evaluation_status'],'error');self.assertIsNone(result['component_measurement_passed'])
        with self.assertRaises(ValueError):evaluation.evaluate(self.root/'other',self.cluster)

    def test_original_timeout_preserves_partial_logs_and_unknown_outcome(self):
        timeout=subprocess.TimeoutExpired('fixture',30,output=b'partial stdout',stderr=b'partial stderr')
        result=self.run_evaluator(side_effect=timeout)
        self.assertTrue(result['original_evaluator_executed']);self.assertIsNone(result['component_measurement_passed'])
        directory=self.root/Path(result['evaluation_ref']).parent
        self.assertEqual((directory/'original.stdout.txt').read_bytes(),b'partial stdout')
        self.assertEqual((directory/'original.stderr.txt').read_bytes(),b'partial stderr')


class InventoryVerifierAuditTests(unittest.TestCase):
    def test_sql_fixture_receipt_cannot_claim_calendar_email_or_runtime_execution(self):
        from integrations import inventory_evaluation_audit as audit
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();path=root/'result.json'
            base={'schema_version':'inventory-evaluator-components-v1','passed':True,
                'checks':[{'name':name,'passed':True} for name in audit.CHECKS],
                'fixture_mode':'explicit_sql_not_original_calendar_or_email_tools',
                **{k:False for k in ('nemo_task_executed','runtime_activation','training_admission','email_tools_called',
                    'calendar_tools_called','reference_files_read','official_benchmark_score_available')}}
            for key in ('email_tools_called','calendar_tools_called','runtime_activation','training_admission','reference_files_read'):
                save(path,{**base,key:True})
                with patch.object(audit,'source_binding') as binding:
                    with self.assertRaises(ValueError):audit.audit(root,path)
                    binding.assert_not_called()

    def test_measurement_wrong_reference_mode_or_task_reward_is_refused_before_inputs(self):
        from integrations import inventory_evaluation_audit as audit
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();profile=EpisodeProfile(root,'f1895a7d-8626-4818-839a-d5ddab57b4a0',0)
            path=root/'result.json'
            for override in ({'reference_mode':'upstream_answer'},{'task_passed':True},{'reward':1}):
                save(path,{'schema_version':'inventory-original-component-measurement-v1','task_id':evaluation.data.TASK_ID,
                    'profile':profile.record(),'evaluation_ref':'result.json','reference_mode':evaluation.REFERENCE_MODE,
                    'task_passed':None,'reward':None,**override})
                with self.assertRaises(ValueError):audit.audit_measurement(root,path,profile)


if __name__=='__main__':unittest.main()
