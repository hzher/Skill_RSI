"""VIP composition, durable intents and independent measurement boundaries."""
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,Mock,patch
from uuid import uuid4

from adapters import vip_email_evaluation as evaluation
from adapters import vip_email_validation as validation
from integrations import vip_email_components as components
from integrations.episode_cluster import EpisodeProfile
from integrations import vip_email_audit as audit
from tests.test_vip_email_validation import customers,outputs
from workenv.core import ResourceError,save


def fixture():
    source=customers();cloud,messages,logs=outputs(source)
    data={'office':{'customers':source,'orders':[],'readonly_sha256':'fixture','spreadsheets':[{'id':'owned','title':cloud['title']}],
                    'sheets':[],'cells':[],'permissions':[],'folders':[]},
          'email':{'messages':messages,'sent_log':logs,'account_config':[],'attachments':[],'drafts':[],'folders':[]}}
    for i,sheet in enumerate(cloud['sheets']):
        data['office']['sheets'].append({'id':str(i),'spreadsheet_id':'owned','title':sheet['title']})
        grid=[sheet['headers'],*[[r[k] for k in sheet['headers']] for r in sheet['rows']]]
        for r,row in enumerate(grid):
            for c,value in enumerate(row):data['office']['cells'].append({'spreadsheet_id':'owned','sheet_id':str(i),'row_index':r,'col_index':c,'value':value,'formula':None})
    return data


class VipBusinessMeasurementTests(unittest.TestCase):
    def test_cloud_grid_maps_owned_complete_tables_to_pure_contract(self):
        self.assertTrue(evaluation.supplement(fixture())['supplemental_passed'])

    def test_sparse_duplicate_formula_and_foreign_cells_fail(self):
        for change in ('sparse','duplicate','formula','foreign'):
            data=fixture();cells=data['office']['cells']
            if change=='sparse':cells.pop()
            elif change=='duplicate':cells.append(deepcopy(cells[0]))
            elif change=='formula':cells[0]['formula']='=SUM(A1:A2)'
            else:cells[0]['spreadsheet_id']='foreign'
            with self.subTest(change=change):self.assertIs(evaluation.supplement(data)['supplemental_passed'],False)

    def test_duplicate_parent_or_tab_is_not_resolved_arbitrarily(self):
        data=fixture();data['office']['spreadsheets'].append(deepcopy(data['office']['spreadsheets'][0]))
        self.assertFalse(evaluation.supplement(data)['supplemental_passed'])
        data=fixture();data['office']['sheets'][1]['title']='Top Customers'
        self.assertFalse(evaluation.supplement(data)['supplemental_passed'])

    def test_original_summary_must_match_exit_code_and_actual_check_lines(self):
        good='  [PASS] fixture\n\n=== SUMMARY ===\n  Passed: 1\n  Failed: 0\n  Overall: PASS\n'
        self.assertIs(evaluation.classify_original(0,good,''),True)
        bad=good.replace('[PASS]','[FAIL]').replace('Passed: 1','Passed: 0').replace('Failed: 0','Failed: 1').replace('Overall: PASS','Overall: FAIL')
        self.assertIs(evaluation.classify_original(1,bad,''),False)
        for code,stdout,stderr in ((1,good,''),(0,good.replace('Passed: 1','Passed: 2'),''),(0,good,'Traceback'),(0,'Overall: PASS','')):
            self.assertIsNone(evaluation.classify_original(code,stdout,stderr))

    def test_original_inventory_rejects_missing_duplicate_and_changed_customer(self):
        data=fixture();source=data['office']['customers'];components.check_customers(source,data)
        for rows in (source[:-1],[*source,source[0]],[{**source[0],'total_spent':'1'},*source[1:]]):
            with self.assertRaises(ResourceError):components.check_customers(rows,data)

    def test_method_arguments_cannot_escape_fixed_business_scope(self):
        for server,name,args in (('woocommerce','woo_customers_delete',{'id':1}),('woocommerce','woo_customers_list',{'page':True,'perPage':10}),
            ('google_sheet','create_spreadsheet',{'title':validation.TITLE,'folder_id':'foreign'}),
            ('google_sheet','update_cells',{'spreadsheet_id':'owned','sheet':'Top Customers','range':'A1:Z99','data':[['x']]}),
            ('google_sheet','update_cells',{'spreadsheet_id':'owned','sheet':'Top Customers','range':'A1:A1','data':[['=IMPORTDATA("private")']]})):
            with self.subTest(name=name),self.assertRaises(ValueError):components.validate_call(server,name,args)

    def test_foreign_cloud_identity_refused_before_original_tool(self):
        with self.assertRaises(ResourceError):components.bind_sheet({'spreadsheet_id':'foreign'},fixture())

    def test_tampered_component_summary_cannot_claim_task_reward(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'result.json'
            save(path,{'schema_version':'vip-original-business-components-diagnostic-v1','passed':True,
                'checks':[{'name':name,'passed':True} for name in audit.CHECKS],'reward':1})
            with self.assertRaises(ValueError):audit.audit_components(root,path)


class VipBusinessRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.profile=EpisodeProfile(self.root,str(uuid4()),0)
        self.cluster=Mock(profile=self.profile,started=True)
        self.controller=components.VipEmailComponents(self.root,self.cluster,{})
        self.controller.run.mkdir(parents=True);save(self.controller.run/'requests.json',self.controller.requests)
        self.controller.opened=True;self.controller.mail=Mock(guard=Mock())
        self.data=fixture();self.controller.initial=deepcopy(self.data)
        save(self.controller.run/'initial_snapshot.json',components.jsonable(self.data))
        self.data=components.jsonable(self.data);self.controller.initial=deepcopy(self.data)
        self.controller.clients={'google_sheet':Mock()}
        self.snap=patch.object(components,'snapshot',return_value=self.data);self.snap.start();self.addCleanup(self.snap.stop)
        self.args={'spreadsheet_id':'owned','sheet':'Top Customers','range':'D2:D2','data':[['1200']]}
        self.reply={'content':[{'type':'text','text':'{"updatedCells":1}'}],'isError':False}

    async def test_rpc_observes_durable_intent_and_replay_uses_cache(self):
        identity=str(uuid4())
        async def rpc(*_):
            saved=json.loads((self.controller.run/'requests.json').read_text())
            self.assertEqual(saved['entries'][identity]['state'],'prepared')
            return Mock(model_dump=lambda **kw:deepcopy(self.reply))
        client=self.controller.clients['google_sheet'];client.call_tool=AsyncMock(side_effect=rpc)
        first=await self.controller.call('google_sheet','update_cells',self.args,identity)
        first['content'][0]['text']='mutated by caller'
        self.assertEqual(await self.controller.call('google_sheet','update_cells',self.args,identity),self.reply)
        with self.assertRaises(ValueError):await self.controller.call('google_sheet','update_cells',{**self.args,'data':[['2']]},identity)
        client.call_tool.assert_awaited_once()

    async def test_original_error_response_retained_and_all_replays_refused(self):
        identity=str(uuid4());self.reply['isError']=True
        client=self.controller.clients['google_sheet'];client.call_tool=AsyncMock(return_value=Mock(model_dump=lambda **kw:self.reply))
        with self.assertRaises(ResourceError):await self.controller.call('google_sheet','update_cells',self.args,identity)
        saved=json.loads((self.controller.run/'requests.json').read_text())['entries'][identity]
        self.assertEqual(saved['state'],'unknown');self.assertTrue(saved['response']['isError']);self.assertIn('after_ref',saved)
        for request in (identity,str(uuid4())):
            with self.assertRaises(ResourceError):await self.controller.call('google_sheet','update_cells',self.args,request)
        with self.assertRaises(ResourceError):evaluation.evaluate(self.controller)
        with self.assertRaisesRegex(ResourceError,'reset refused'):evaluation.reset(self.root,self.cluster)
        client.call_tool.assert_awaited_once()

    async def test_cancellation_after_dispatch_preserves_unknown_intent(self):
        import asyncio
        identity=str(uuid4());client=self.controller.clients['google_sheet'];client.call_tool=AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(ResourceError):await self.controller.call('google_sheet','update_cells',self.args,identity)
        self.assertEqual(json.loads((self.controller.run/'requests.json').read_text())['entries'][identity]['state'],'unknown')

    async def test_prepared_intent_or_changed_source_blocks_new_dispatch(self):
        self.controller.clients['google_sheet'].call_tool=AsyncMock()
        self.controller.requests['entries'][str(uuid4())]={'state':'prepared'};save(self.controller.run/'requests.json',self.controller.requests)
        with self.assertRaises(ResourceError):await self.controller.call('google_sheet','update_cells',self.args,str(uuid4()))
        self.controller.requests['entries'].clear();save(self.controller.run/'requests.json',self.controller.requests)
        self.data['office']['readonly_sha256']='changed'
        with self.assertRaises(ResourceError):await self.controller.call('google_sheet','update_cells',self.args,str(uuid4()))
        self.controller.clients['google_sheet'].call_tool.assert_not_awaited()

    async def test_three_children_required_before_combined_closed_checkpoint(self):
        self.controller.children={'email':{'closed':True},'woocommerce':{'closed':True}}
        self.controller.checkpoint('closed')
        self.assertEqual(json.loads((self.profile.base/'dependent_processes.json').read_text())['state'],'unknown')
        self.controller.children['google_sheet']={'closed':True};self.controller.checkpoint('closed')
        self.assertEqual(json.loads((self.profile.base/'dependent_processes.json').read_text())['state'],'closed')


class VipBusinessAuditTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.profile=EpisodeProfile(self.root,str(uuid4()),0)
        self.run=self.profile.base/'vip-components-run';self.run.mkdir(parents=True)
        self.data=components.jsonable(fixture())
        save(self.run/'initial_snapshot.json',self.data)
        save(self.run/'original_customers.json',{'customers':self.data['office']['customers'],'readonly_sha256':'fixture'})
        self.script=b'# pinned evaluator fixture, never executed\n';self.sha=hashlib.sha256(self.script).hexdigest()
        fake_pin=patch.dict(audit.PUBLIC_SHA256,{audit.BASE+'evaluation/main.py':self.sha});fake_pin.start();self.addCleanup(fake_pin.stop)
        truth=validation.expected(self.data['office']['customers'],source_binding=validation.source_identity())
        sql=components.jsonable({'rows':truth['rows'],'metrics':truth['metrics']})
        controller=Mock(root=self.root,cluster=Mock(profile=self.profile),run=self.run)
        result=Mock(returncode=0,stdout=b'  [PASS] fixture\n=== SUMMARY ===\n  Passed: 1\n  Failed: 0\n  Overall: PASS\n',stderr=b'')
        with patch.object(evaluation,'snapshot',return_value=self.data),patch.object(evaluation,'sql_truth',return_value=sql),\
             patch.object(evaluation,'public_blob',return_value=(self.script,{'path':audit.BASE+'evaluation/main.py','sha256':self.sha})),\
             patch.object(evaluation.subprocess,'run',return_value=result):
            self.measured=evaluation.evaluate(controller)

    def test_original_logs_sql_and_supplement_are_independently_recomputed(self):
        self.assertEqual(audit.audit_measurement(self.root,self.measured),(True,True))

    def test_rewritten_summary_cannot_assign_a_task_reward(self):
        self.measured['reward']=1;save(self.root/self.measured['evaluation_ref'],self.measured)
        with self.assertRaisesRegex(ValueError,'recomputation'):audit.audit_measurement(self.root,self.measured)

    def test_original_customer_bytes_cannot_change_after_evaluation(self):
        save(self.run/'original_customers.json',{'customers':[],'readonly_sha256':'fixture'})
        with self.assertRaisesRegex(ValueError,'inventory changed'):audit.audit_measurement(self.root,self.measured)

    def test_snapshot_cannot_be_rebound_to_another_resource(self):
        self.measured['snapshot_ref']='evidence/another.json'
        with self.assertRaisesRegex(ValueError,'another resource'):audit.audit_measurement(self.root,self.measured)

    def test_compact_snapshot_roundtrips_and_binds_original_source_bytes(self):
        path=self.root/self.measured['snapshot_ref'];source=self.run/'initial_snapshot.json'
        self.assertEqual(components.load_snapshot(self.root,path,source),self.data)
        record=json.loads(path.read_text());self.assertNotIn('orders',record['state']['office'])
        source.write_text(source.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'source bytes changed'):components.load_snapshot(self.root,path,source)

    def test_compact_snapshot_cannot_hide_changed_source_rows(self):
        changed=deepcopy(self.data);changed['office']['customers'][0]['total_spent']='999999'
        with self.assertRaises(ResourceError):components.save_snapshot(self.root,self.run/'bad.json',changed,self.run/'initial_snapshot.json')
