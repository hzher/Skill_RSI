"""Cloud/mail worker preparation: identity, unknown outcomes and retained state."""
from copy import deepcopy
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from integrations import vip_email_resource as resource
from integrations import vip_email_components as components
from integrations.email_components import EmailComponents
from integrations.vip_resource_audit import audit_closed_state
from integrations.episode_cluster import EpisodeProfile
from workenv.core import ResourceError, save


def measurement(original=True, supplemental=True):
    return {"schema_version": "vip-original-database-component-measurement-v1", "task_id": resource.TASK_ID,
        "reference_mode": resource.REFERENCE_MODE, "runtime_task_activated": False, "training_eligible": False,
        "upstream_reference_used": False, "official_benchmark_score_available": False, "task_passed": None,
        "reward": None, "external_messages_sent": 0, "source_completeness_verified": True,
        "owned_database_snapshot_verified": True, "original_passed": original,
        "supplemental": {"supplemental_passed": supplemental},
        "component_measurement_passed": None if original is None or supplemental is None else original and supplemental,
        "evaluation_ref": "measurements/result.json"}


class VipResourceClassificationTests(unittest.TestCase):
    def test_close_rejects_changed_content_even_when_mail_count_is_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            profile=EpisodeProfile(Path(temp),str(uuid4()),0)
            data={'email':{'messages':[{'id':1,'body_text':'original'}],'sent_log':[{'id':1}]},'office':{'cells':[]}}
            record={'profile':profile.record(),'reset_performed':False,'dependent_processes_closed':True,
                'external_messages_sent':0,'snapshot_ref':(profile.base/'vip-task-session/closed_snapshot.json').relative_to(profile.owner).as_posix()}
            audit_closed_state(profile,record,data,deepcopy(data))
            changed=deepcopy(data);changed['email']['messages'][0]['body_text']='same-count forgery'
            with self.assertRaises(ValueError):audit_closed_state(profile,record,changed,data)
            with self.assertRaises(ValueError):audit_closed_state(profile,{**record,'reset_performed':True},data,data)

    def test_unknown_is_never_converted_to_business_failure(self):
        for original,supplemental in ((None,True),(True,None),(None,False),(False,None)):
            result = resource.episode_result(measurement(original,supplemental))
            self.assertIsNone(result['task_passed']); self.assertEqual(result['evaluation_status'],'error')

    def test_original_blind_spot_is_diagnostic_failure(self):
        for original,supplemental in ((True,False),(False,True),(False,False),(True,True)):
            result = resource.episode_result(measurement(original,supplemental))
            self.assertIs(result['task_passed'],original and supplemental)
            self.assertEqual(result['reward_version'],resource.REWARD_VERSION)
            self.assertFalse(result['training_eligible'])

    def test_missing_source_scope_or_forged_classification_refused(self):
        for field,value in (('source_completeness_verified',False),('runtime_task_activated',True),
            ('training_eligible',True),('external_messages_sent',False),('component_measurement_passed',False),
            ('reference_mode','official'),('original_passed',1),('reward',1)):
            record=measurement();record[field]=value
            with self.subTest(field=field),self.assertRaises(ResourceError):resource.episode_result(record)

    def test_request_namespaces_are_deterministic_disjoint_and_resource_scoped(self):
        rid,other,outer=(str(uuid4()) for _ in range(3))
        expected=resource.request_identity(rid,outer)
        self.assertEqual(resource.request_identity(rid,outer),expected)
        self.assertNotEqual(resource.request_identity(other,outer),expected)
        self.assertNotEqual(resource.request_identity(rid,outer,purpose='source'),expected)
        for a,b in ((rid,outer.upper()),('not-a-uuid',outer),(rid,123)):
            with self.assertRaises(ValueError):resource.request_identity(a,b)

    def test_standalone_snapshots_keep_full_format_combined_snapshots_reconstruct(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); source=root/'initial.json'; full=root/'full.json'; compact=root/'compact.json'
            data={'office':{'customers':[{'id':1}], 'orders':[{'id':2}], 'readonly_sha256':'fixture','cells':[]},'email':{'messages':[]}}
            save(source,data)
            EmailComponents.save_request_snapshot(Mock(),full,data)
            owner=Mock(root=root,run=root)
            save(root/'initial_snapshot.json',data)
            components._CombinedEmail.save_request_snapshot(Mock(owner=owner),compact,data)
            self.assertEqual(json.loads(full.read_text()),data)
            self.assertNotIn('customers',json.loads(compact.read_text())['state']['office'])
            self.assertEqual(components.load_snapshot(root,compact,root/'initial_snapshot.json'),data)
            save(root/'initial_snapshot.json',{**data,'email':{'messages':[{'id':1}]}})
            with self.assertRaises(ValueError):components.load_snapshot(root,compact,root/'initial_snapshot.json')


class VipResourceAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.profile=EpisodeProfile(self.root,str(uuid4()),0)
        self.profile.base.mkdir(parents=True)
        self.cluster=Mock(profile=self.profile)
        self.adapter=resource.VipEmailResourceAdapter(self.root,self.cluster,build_record={})
        self.run=self.adapter.new_run()
        self.adapter.context=Mock(__aexit__=AsyncMock())
        self.controller=Mock(opened=True,unknown=False,mail=Mock(unknown=False))
        self.controller.run=self.profile.base/'vip-components-run';self.controller.run.mkdir()
        self.adapter.components=self.controller

    async def test_outer_identity_is_durable_before_component_call_and_reused(self):
        outer=str(uuid4());trace=[];internal=resource.request_identity(self.profile.resource_id,outer)
        async def call(server,name,args,identity):
            saved=json.loads((self.run/'request_bindings.json').read_text())
            self.assertEqual(saved['entries']['tool/'+outer]['internal_request_id'],identity)
            return {'content':[],'isError':False}
        self.controller.call=AsyncMock(side_effect=call)
        for _ in range(2):await self.adapter.call_request('email','send_email',{'body':'fixture'},outer,trace)
        self.assertEqual([c.args[-1] for c in self.controller.call.await_args_list],[internal,internal])
        self.assertEqual(len(self.adapter.bindings['entries']),1)
        self.assertEqual(trace[0]['external_request_id'],outer)

    async def test_legacy_call_cannot_create_random_identity_or_dispatch(self):
        self.controller.call=AsyncMock()
        with self.assertRaises(ValueError):await self.adapter.call('email','send_email',{},[])
        self.controller.call.assert_not_awaited()

    async def test_restored_source_must_match_bound_runtime_seed_before_ready(self):
        self.adapter.context=None;self.adapter.runtime_seed={'readonly_sha256':'expected'}
        context=Mock(__aenter__=AsyncMock(),__aexit__=AsyncMock())
        self.controller.open.return_value=context
        self.controller.schemas=[{'server':s,'name':n} for s,names in components.METHODS.items() for n in names]
        with patch.object(resource,'snapshot',return_value={'office':{'readonly_sha256':'foreign'}}):
            with self.assertRaisesRegex(ResourceError,'restored read-only'):await self.adapter.seed(self.run)
        context.__aexit__.assert_awaited_once();self.assertIsNone(self.adapter.context)

    async def test_unknown_blocks_new_bindings_calls_and_measurement(self):
        self.controller.guard.side_effect=ResourceError('outcome unknown')
        self.controller.call=AsyncMock()
        with self.assertRaises(ResourceError):await self.adapter.call_request('email','send_email',{},str(uuid4()),[])
        with self.assertRaises(ResourceError):await self.adapter.verify()
        self.controller.call.assert_not_awaited();self.assertEqual(self.adapter.bindings['entries'],{})

    async def test_changed_binding_file_refuses_dispatch(self):
        self.controller.call=AsyncMock();save(self.run/'request_bindings.json',{'entries':{}})
        with self.assertRaises(ResourceError):await self.adapter.call_request('email','send_email',{},str(uuid4()),[])
        self.controller.call.assert_not_awaited()

    async def test_cloud_mail_inventory_has_no_artifact_write_surface(self):
        self.controller.schemas=[{'server':s,'name':n} for s,names in components.METHODS.items() for n in names]
        tools=self.adapter.tools();self.assertEqual(len(tools['tools']),10);self.assertEqual(tools['artifact_tools'],[])
        tools['tools'].clear();self.assertEqual(len(self.controller.schemas),10)
        for operation in (self.adapter.read(),self.adapter.write({})):
            with self.assertRaises(ValueError):await operation

    @unittest.skipUnless(importlib.util.find_spec('jsonschema'),'Optional model task schema validator')
    async def test_published_scope_accepts_controller_arguments_and_rejects_wider_mcp(self):
        from jsonschema import Draft202012Validator
        valid=[('woocommerce','woo_customers_list',{'page':1,'perPage':10}),
               ('email','get_emails',{'folder':'Sent','page':1,'page_size':50}),
               ('email','send_email',{'to':'office@example.com','subject':'Report','body':'Report contents'}),
               ('google_sheet','get_sheet_data',{'spreadsheet_id':'owned','sheet':'Store Summary','range':'A1:B5'}),
               ('google_sheet','update_cells',{'spreadsheet_id':'owned','sheet':'Top Customers','range':'A1:B1','data':[['Name',1]]})]
        for server,name,args in valid:
            with self.subTest(server=server,name=name):
                Draft202012Validator(resource.scoped_tool_schema(server,name)).validate(args)
                components.validate_call(server,name,args)
        invalid=[('woocommerce','woo_customers_list',{'page':1,'perPage':100}),
                 ('woocommerce','woo_customers_list',{'page':1,'perPage':10,'orderby':'id'}),
                 ('email','get_emails',{'folder':'Inbox','page':1,'page_size':10}),
                 ('google_sheet','get_sheet_data',{'spreadsheet_id':'owned','sheet':'Store Summary','range':'A1:E11'})]
        for server,name,args in invalid:
            with self.subTest(server=server,name=name):
                self.assertFalse(Draft202012Validator(resource.scoped_tool_schema(server,name)).is_valid(args))

    async def test_scoped_discovery_does_not_mutate_original_schemas(self):
        original=[{'server':s,'name':n,'inputSchema':{'fixture':'original'}} for s,names in components.METHODS.items() for n in names]
        self.controller.schemas=original
        tools=self.adapter.tools()['tools']
        self.assertEqual(self.controller.schemas,original)
        self.assertEqual(tools[0]['inputSchema']['properties']['perPage']['const'],10)

    async def test_verify_collects_source_once_in_disjoint_namespace(self):
        async def collect(controller,factory):
            ids=[factory(),factory()]
            self.assertEqual(len(set(ids)),2)
            save(controller.run/'original_customers.json',{'fixture':True})
        with patch.object(resource,'collect_sources',new=AsyncMock(side_effect=collect)) as collect_mock,patch.object(resource,'evaluate',return_value=measurement()):
            for _ in range(2):
                result,path=await self.adapter.verify();self.assertTrue(result['task_passed']);self.assertTrue(path.is_file())
            collect_mock.assert_awaited_once()
        self.assertEqual({e['purpose'] for e in self.adapter.bindings['entries'].values()},{'source'})

    async def test_close_unknown_preserves_state_and_never_initializes(self):
        self.controller.unknown=True
        data={'office':{'customers':[],'orders':[],'readonly_sha256':'fixture','cells':[]},'email':{'messages':[{'id':1}]}}
        save(self.controller.run/'initial_snapshot.json',data)
        context=self.adapter.context
        with patch.object(resource,'snapshot',return_value=data),patch.object(resource,'require_dependents_closed') as closed:
            await self.adapter.close(self.run)
            context.__aexit__.assert_awaited_once();closed.assert_called_once_with(self.profile)
        record=json.loads((self.run/'closed.json').read_text())
        self.assertTrue(record['outcome_unknown']);self.assertFalse(record['reset_performed'])
        self.assertEqual(components.load_snapshot(self.root,self.run/'closed_snapshot.json',self.controller.run/'initial_snapshot.json'),data)
        self.controller.initialize.assert_not_called()

    async def test_live_dependents_prevent_claiming_resource_closed(self):
        with patch.object(resource,'require_dependents_closed',side_effect=ResourceError('still alive')):
            with self.assertRaises(ResourceError):await self.adapter.close(self.run)
        self.assertFalse((self.run/'closed.json').exists())

    async def test_foreign_run_cannot_close_or_reopen_resource(self):
        with self.assertRaises(ResourceError):await self.adapter.close(self.root/'foreign')
        with self.assertRaises(ResourceError):self.adapter.new_run()
        with self.assertRaises(ResourceError):await self.adapter.seed(self.root/'foreign')


if __name__=='__main__':unittest.main()
