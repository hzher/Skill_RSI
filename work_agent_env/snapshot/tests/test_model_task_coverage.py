"""Every seed remains visible; unattempted prerequisites are not model failures."""
import json
from pathlib import Path
import tempfile
import unittest
from scripts.report_model_task_coverage import collect,diagnose

class ModelCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        def write(name,value):
            path=self.root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(value))
        self.write=write
        write('training/toolathlon_seed_catalog.json',{'commit':'unit','directories':[{'task_id':t,'source_tree':'tree'} for t in ('tax','other')]})
        write('training/toolathlon_seed_capabilities.json',{'tasks':{
            'tax':{'config_sha256':'a','needed_mcp_servers':['woocommerce'],'needed_local_tools':['python_execute'],'file_asset_count':0},
            'other':{'config_sha256':'b','needed_mcp_servers':['notion','filesystem'],'needed_local_tools':['python_execute'],'file_asset_count':2}}})
        write('configs/task_adapter_profiles.json',{'profiles':{'tax':{'task_id':'tax','resource_backends':['isolated']}}})
        path=self.root/'scripts/report_model_task_coverage.py'; path.parent.mkdir(); path.write_text('unit source')

    def receipt(self,status,closed=True):
        return {'schema_version':'nemo-model-task-v1','training_updates':0,'official_score_available':False,'task_id':'tax',
                'started_at':'2026-10-22T00:00:00+00:00','requested_model':'unit-model','run_id':'unit','status':status,
                'failure_kind':None,'closed':closed,'model_http_requests':3}

    def test_all_seeds_including_missing_dependencies_are_accounted_for(self):
        report=collect(self.root,native_preflight={'cli_available':False})
        self.assertEqual(report['summary']['seed_tasks'],2)
        self.assertEqual(report['summary']['tasks_not_yet_attempted'],2)
        self.assertEqual(report['tasks'][1]['additional_services'],['notion'])
        self.assertEqual(report['tasks'][1]['model_execution_status'],'requires_adaptation')
        self.assertNotIn('business_failed',report['summary']['attempts_by_status'])

    def test_unknown_and_failed_runs_never_count_as_success(self):
        self.write('evidence/model_tasks/a.json',self.receipt('unknown'))
        self.write('evidence/model_tasks/b.json',self.receipt('business_failed'))
        report=collect(self.root)
        self.assertEqual(report['summary']['model_attempts'],2)
        self.assertEqual(report['summary']['tasks_with_any_passed_model_attempt'],0)

    def test_success_requires_closed_resources(self):
        self.write('evidence/model_tasks/a.json',self.receipt('passed',False))
        self.assertEqual(collect(self.root)['summary']['tasks_with_any_passed_model_attempt'],0)
        self.write('evidence/model_tasks/a.json',self.receipt('passed',True))
        self.assertEqual(collect(self.root)['summary']['tasks_with_any_passed_model_attempt'],1)

    def test_diagnostics_export_only_known_metadata_and_no_argument_or_error_body(self):
        identity='a'*32
        row=self.receipt('unknown');row['run_id']=identity
        self.write('evidence/model_tasks/a.json',row)
        path=self.root/'.runtime/model-task-runs'/identity/'trace.jsonl';path.parent.mkdir(parents=True)
        events=[{'kind':'tool_intent','value':{'name':'woocommerce__woo_customers_list','arguments':{'private':'DO_NOT_EXPORT'}}},
                {'kind':'nemo_error','value':{'status':400,'route':'/isolated/tool','response':{'detail':'VIP customer pagination scope differs','private':'DO_NOT_EXPORT'}}},
                {'kind':'model_response','value':{'text':'DO_NOT_EXPORT'}},
                {'kind':'tool_validation_rejected','value':{'private':'DO_NOT_EXPORT'}}]
        path.write_text('\n'.join(json.dumps(e) for e in events))
        result=diagnose(self.root);text=json.dumps(result)
        self.assertNotIn('DO_NOT_EXPORT',text)
        self.assertEqual(result['attempts'][0]['nemo_http_status'],400)
        self.assertEqual(result['attempts'][0]['schema_rejections'],1)
        self.assertEqual(result['attempts'][0]['diagnosis'],'published_customer_schema_exceeds_runtime_scope')

    def test_diagnostics_missing_private_data_remains_unobserved(self):
        row=self.receipt('unknown');row['run_id']='b'*32
        self.write('evidence/model_tasks/b.json',row)
        result=diagnose(self.root)['attempts'][0]
        self.assertFalse(result['private_trace_available'])
        self.assertNotIn('nemo_http_status',result)

    def test_operator_switch_is_visible_without_becoming_model_failure_or_success(self):
        row=self.receipt('unknown');row.update(run_id='c'*32,operator_interrupted=True,operator_stop_reason='switch_to_tested_repair_package')
        self.write('evidence/model_tasks/c.json',row)
        report=collect(self.root)
        self.assertEqual(report['summary']['operator_interrupted_attempts'],1)
        self.assertEqual(report['summary']['tasks_with_any_passed_model_attempt'],0)
        self.assertTrue(report['tasks'][0]['latest_by_requested_model']['unit-model']['operator_interrupted'])
        self.assertEqual(diagnose(self.root)['attempts'][0]['operator_stop_reason'],'switch_to_tested_repair_package')

if __name__=='__main__': unittest.main()
