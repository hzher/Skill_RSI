"""Inventory source proof must precede business measurements or activation."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations import inventory_data as data
from scripts import prepare_inventory_data_seed as seed
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile
from tests.test_inventory_alert_validation import products
from workenv.core import ResourceError, save


def snapshot_fixture():
    rows=products()
    tables={'wc.products':data.row_summary(rows)}
    return {'products':rows,'events':[],'email':{n:[] for n in data.EMAIL_TABLES},
        'readonly_tables':tables,'readonly_sha256':data.checksum(tables),
        'sql_zero_stock':[{'id':7,'name':'Mug','sku':'','category':'Uncategorized'},
            {'id':42,'name':'Tea - Large','sku':'T42','category':'Food'}]}


def response(rows):return {'content':[{'type':'text','text':json.dumps(rows)}],'isError':False}


class InventoryDataTests(unittest.TestCase):
    def test_sql_and_python_disagreement_never_becomes_business_failure(self):
        fixture=snapshot_fixture();self.assertEqual(len(data.validate_snapshot(fixture)['products']),2)
        fixture['sql_zero_stock'][0]['category']='Other'
        with self.assertRaises(ResourceError):data.validate_snapshot(fixture)

    def test_product_rows_must_match_full_readonly_table_digest(self):
        fixture=snapshot_fixture();fixture['products'][0]['price']='new non-projected field'
        with self.assertRaises(ResourceError):data.validate_snapshot(fixture)
        fixture=snapshot_fixture();fixture['readonly_tables']['wc.products']['count']+=1
        with self.assertRaises(ResourceError):data.validate_snapshot(fixture)

    def test_catalog_projection_retains_critical_fields_and_does_not_mutate(self):
        original=products();before=deepcopy(original)
        projected=data.catalog_projection(original)
        self.assertEqual([p['id'] for p in projected],[7,11,21,42]);self.assertEqual(original,before)
        changed=deepcopy(original);changed[0]['categories'].reverse()
        self.assertNotEqual(data.catalog_projection(changed),projected)

    def test_row_digest_is_order_independent_and_full_field_sensitive(self):
        original=products();self.assertEqual(data.row_summary(original),data.row_summary(list(reversed(original))))
        changed=deepcopy(original);changed[0]['nonprojected']='change'
        self.assertNotEqual(data.row_summary(original),data.row_summary(changed))

    def test_error_nonjson_wrong_page_shape_and_oversize_are_unknown(self):
        for reply in ({'isError':True},{'content':[]},{'content':[{'type':'text','text':'{}'}]},
            response([products()[0]]*11),{'content':[{'type':'text','text':'x'*(4*1024*1024+1)}]}):
            with self.assertRaises(ResourceError):data.decode_products(reply)

    def test_page_audit_rejects_omission_duplicate_forged_filter_and_early_end(self):
        entry={'name':'woo_products_list','arguments':{'page':1,'perPage':10},'response':response(products())}
        self.assertEqual(data.audit_pages([entry],products()),{'pages':1,'products':4})
        for entries in ([],[entry,entry], [{**entry,'arguments':{'page':2,'perPage':10}}],
            [{**entry,'response':response(products()[:-1])}],
            [{**entry,'response':response([products()[0]]*4)}],
            [{**entry,'arguments':{'page':1,'perPage':10,'stock_status':'outofstock'}}]):
            with self.assertRaises((ValueError,ResourceError)):data.audit_pages(entries,products())


class InventoryPagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_page_multiple_requires_empty_terminal_page(self):
        rows=[{**products()[0],'id':i+1} for i in range(10)];calls=[]
        async def call(name,args):
            calls.append((name,args));return response(rows if args['page']==1 else [])
        actual,journal=await data.collect_products(call,rows)
        self.assertEqual(actual,rows);self.assertEqual(len(calls),2)
        self.assertEqual(data.audit_pages(journal,rows),{'pages':2,'products':10})

    async def test_short_page_cannot_hide_missing_or_changed_sql_products(self):
        async def call(name,args):return response(products()[:-1])
        with self.assertRaises(ResourceError):await data.collect_products(call,products())
        async def changed(name,args):
            rows=products();rows[0]['sku']='other';return response(rows)
        with self.assertRaises(ResourceError):await data.collect_products(changed,products())

    async def test_duplicate_or_invalid_page_stops_without_retry(self):
        calls=[]
        async def call(name,args):calls.append(args);return response([products()[0]]*10)
        with self.assertRaises(ResourceError):await data.collect_products(call,products())
        self.assertEqual(len(calls),1)

    async def test_unterminated_pages_are_bounded_and_not_replayed(self):
        calls=[]
        async def call(name,args):
            calls.append(args['page'])
            return response([{**products()[0],'id':(args['page']-1)*10+i+1} for i in range(10)])
        with patch.object(data,'MAX_PAGES',2):
            with self.assertRaises(ResourceError):await data.collect_products(call,products())
        self.assertEqual(calls,[1,2])


class InventoryInitializationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.profile=EpisodeProfile(self.root,'f1895a7d-8626-4818-839a-d5ddab57b4a0',0)
        self.profile.base.mkdir(parents=True);save(self.profile.base/'cluster_owner.json',self.profile.record())
        self.cluster=EpisodeCluster(self.profile);self.cluster.started=True

    def initialize(self,before,after,result=0):
        from types import SimpleNamespace
        with patch.object(data,'snapshot',side_effect=[before,after]),patch.object(data,'public_blob',return_value=(b'# fixed fixture',{'sha256':'a'*64})),\
             patch.object(data.subprocess,'run',return_value=SimpleNamespace(returncode=result,stdout=b'fixture',stderr=b'')):
            return data.initialize(self.root,self.cluster)

    def test_ready_marker_has_independent_inventory_task_and_contract_identity(self):
        self.initialize(snapshot_fixture(),snapshot_fixture())
        record=json.loads((self.profile.base/'inventory-initialized.json').read_text())
        self.assertEqual(record['task_id'],'wc-inventory-alert-gcal')
        self.assertEqual(record['contract_source'],data.validation.source_identity())
        self.assertEqual(record['state'],'ready')
        self.assertFalse((self.profile.base/'email-initialized.json').exists())

    def test_failed_preprocess_or_changed_source_never_leaves_ready_marker(self):
        for kind in ('exit','readonly','events','email'):
            before=snapshot_fixture();after=deepcopy(before)
            if kind=='readonly':after['readonly_sha256']='other'
            if kind=='events':after['events']=[{'id':'unexpected'}]
            if kind=='email':after['email']['messages']=[{'id':1}]
            with self.assertRaises(ResourceError):self.initialize(before,after,1 if kind=='exit' else 0)
            self.assertEqual(json.loads((self.profile.base/'inventory-initialized.json').read_text())['state'],'initializing')

    def test_foreign_or_stopped_cluster_refused_before_preprocess(self):
        with patch.object(data.subprocess,'run') as run:
            self.cluster.started=False
            with self.assertRaises(ResourceError):data.initialize(self.root,self.cluster)
            self.cluster.started=True
            with self.assertRaises(ResourceError):data.initialize(self.root/'other',self.cluster)
            run.assert_not_called()

    def test_pending_file_intent_blocks_database_reset_before_dispatch(self):
        workspace=self.profile.base/'inventory-workspace';workspace.mkdir()
        save(self.profile.base/(data.artifacts.PREFIX+'0'*32+'.json'),{'state':'prepared'})
        with patch.object(data.subprocess,'run') as run:
            with self.assertRaises(ResourceError):data.initialize(self.root,self.cluster,resetting=True)
            run.assert_not_called()

    def test_unknown_email_intent_blocks_direct_inventory_reset(self):
        from uuid import uuid4
        from integrations.email_task_identity import EmailTaskIdentity
        for state in ('prepared', 'unknown'):
            task = EmailTaskIdentity(data.TASK_ID)
            path = self.profile.base / 'email-components-run/requests.json'
            save(path, {'schema_version': 'owned-email-request-journal-v1', 'profile': self.profile.record(),
                'task_id': task.task_id, 'sender': task.sender, 'entries': {str(uuid4()): {'state': state}}})
            before = path.read_bytes()
            with patch.object(data.subprocess, 'run') as run, self.assertRaises(ResourceError):
                data.initialize(self.root, self.cluster, resetting=True)
            run.assert_not_called(); self.assertEqual(path.read_bytes(), before)


class InventorySeedTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.sql=self.root/'.runtime/native-episodes/seed-cache/fixture.sql';self.sql.parent.mkdir(parents=True)
        self.sql.write_bytes(b'CREATE SCHEMA wc;')
        import hashlib
        self.record={'schema_version':'inventory-data-seed-v1','task_id':seed.TASK_ID,'source_commit':seed.PIN,
            'task_config_sha256':'a'*64,'schemas':list(data.SCHEMAS),'writable_rows_included':False,
            'runtime_activation':False,'training_admission':False,'seed_ref':self.sql.relative_to(self.root).as_posix(),
            'sha256':hashlib.sha256(self.sql.read_bytes()).hexdigest(),'bytes':self.sql.stat().st_size,
            'source_state':{'wc.products':{'count':0,'sha256':'b'*64}}}
        self.record['source_state_sha256']=data.checksum(self.record['source_state'])
        self.source=patch.object(seed,'public_blob',return_value=(b'fixture',{'sha256':'a'*64}));self.source.start();self.addCleanup(self.source.stop)

    def test_exact_seed_validates_without_activating_any_runtime(self):
        self.assertEqual(seed.validate_seed(self.root,self.record),self.sql)

    def test_foreign_task_source_schema_activation_and_bytes_rejected(self):
        for key,value in (('task_id','other'),('source_commit','0'*40),('task_config_sha256','0'*64),
            ('schemas',['wc','email']),('runtime_activation',True),('training_admission',True),
            ('bytes',1),('source_state_sha256','0'*64)):
            with self.assertRaises(ResourceError):seed.validate_seed(self.root,{**self.record,key:value})

    def test_writable_seed_rows_rejected_even_with_matching_digest(self):
        import hashlib
        for schema in ('email','gcal'):
            raw=('COPY '+schema+'.events FROM stdin;').encode();self.sql.write_bytes(raw)
            record={**self.record,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
            with self.assertRaises(ResourceError):seed.validate_seed(self.root,record)

    def test_path_outside_seed_cache_is_rejected_before_restoration(self):
        other=self.root/'external.sql';other.write_bytes(self.sql.read_bytes())
        with self.assertRaises(ResourceError):seed.validate_seed(self.root,{**self.record,'seed_ref':'external.sql'})


class InventoryAuditGuardTests(unittest.TestCase):
    def setUp(self):
        from integrations import inventory_data_audit
        self.audit=inventory_data_audit
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.receipt={'schema_version':'inventory-data-components-v1','passed':True,'task_id':data.TASK_ID,
            'task_passed':None,'reward':None,'real_model_calls':0,'training_updates':0,
            'checks':[{'name':n,'passed':True} for n in self.audit.CHECKS],
            **{k:False for k in ('nemo_task_executed','runtime_activation','training_admission','email_tools_called',
                'calendar_tools_called','original_evaluator_executed','reference_files_read','official_benchmark_score_available')}}
        self.path=self.root/'receipt.json'

    def test_component_success_cannot_promote_training_task_reward_or_tool_claims(self):
        for key,value in (('task_passed',True),('reward',1),('real_model_calls',1),('training_updates',True),
            ('runtime_activation',True),('email_tools_called',True),('calendar_tools_called',True),
            ('original_evaluator_executed',True),('official_benchmark_score_available',True),('task_id','other')):
            save(self.path,{**self.receipt,key:value})
            with patch.object(self.audit,'source_binding') as binding:
                with self.assertRaises(ValueError):self.audit.audit(self.root,self.path)
                binding.assert_not_called()

    def test_missing_check_cannot_be_replaced_by_a_true_pass_flag(self):
        save(self.path,{**self.receipt,'checks':self.receipt['checks'][:-1]})
        with self.assertRaises(ValueError):self.audit.audit(self.root,self.path)

    def test_retained_paths_cannot_read_personal_reports_or_task_answers(self):
        for forbidden in ('weekly_report.md','.upstream/toolathlon-verified/task.md','tasks/answer.txt'):
            value={**self.receipt,'source_sha256':{n:'0'*64 for n in self.audit.SOURCES},
                'retained_sha256':{forbidden:'0'*64,**{f'.runtime/native-episodes/fixture/{i}.json':'0'*64 for i in range(19)}}}
            save(self.path,value)
            with patch.object(self.audit,'source_binding'):
                with self.assertRaisesRegex(ValueError,'outside data-component evidence'):self.audit.audit(self.root,self.path)

    def test_public_constant_comparison_uses_ast_without_executing_source(self):
        sentinel=self.root/'must-not-exist'
        raw=("from pathlib import Path\nPath("+repr(str(sentinel))+").write_text('executed')\nOOS_PRODUCT_IDS=[3,8]\n").encode()
        with patch.object(self.audit,'public_blob',return_value=(raw,{})):
            self.assertEqual(self.audit.constant_ids(self.root),[3,8])
        self.assertFalse(sentinel.exists())


if __name__=='__main__':unittest.main()
