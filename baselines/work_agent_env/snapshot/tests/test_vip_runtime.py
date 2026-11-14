"""Runtime profile, SQL/build binding and explicit bridge request handoff."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from integrations.task_profiles import IMPLEMENTATION_FILES, build_registry
from integrations.task_routing import TaskRouter
from integrations.vip_email_resource import REFERENCE_MODE, REWARD_VERSION
from integrations.vip_seed import validated_seed, SEED_REF, TASK_ID, PIN, PUBLIC_SHA256, BASE
from workenv.core import ResourceError, save

ROOT=Path(__file__).resolve().parents[1]


class VipRuntimeProfileTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        for name in {*IMPLEMENTATION_FILES,'configs/mainline_sources.lock.json','training/toolathlon_seed_capabilities.json'}:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((ROOT/name).read_bytes())
        self.registry=build_registry(self.root);save(self.root/'configs/task_adapter_profiles.json',self.registry)
        self.seed={'resources_session_id':str(uuid4()),'episode_id':{'rollout_id':'fixture','attempt':0},
            'task_id':{'taskset':'toolathlon_gym:fixture','task_id':TASK_ID},'task_data':{}}

    def test_cloud_mail_profile_uses_ten_original_methods_and_no_xlsx(self):
        route=TaskRouter(self.root,'isolated').authorize(self.seed)
        self.assertEqual(sum(map(len,route['mcp_methods'].values())),10)
        self.assertEqual(route['artifact_tools'],[]);self.assertEqual(route['measurement_mode'],REFERENCE_MODE)
        self.assertEqual(route['reward_version'],REWARD_VERSION);self.assertFalse(route['training_admission'])
        self.assertEqual(route['original_service_aliases'],{'email':'emails'})
        self.assertEqual(self.registry['profiles'][TASK_ID+'-v1']['sql_schemas'],['wc','gsheet','email'])

    def test_shared_backend_cannot_allocate_mail_task(self):
        with self.assertRaises(ResourceError):TaskRouter(self.root,'shared').authorize(self.seed)
        self.assertFalse((self.root/'.runtime').exists())

    def test_changed_email_dependency_or_uuid_component_invalidates_profile(self):
        for name in ('configs/email.requirements.lock.txt','integrations/vip_email_resource.py'):
            router=TaskRouter(self.root,'isolated');path=self.root/name;original=path.read_bytes()
            path.write_bytes(original+b'\n# changed fixture\n')
            with self.assertRaises(ResourceError):router.authorize(self.seed)
            path.write_bytes(original)


class VipRuntimeSeedTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.raw=b'-- immutable wc plus empty gsheet and email schema fixture\n'
        self.path=self.root/'.runtime/native-episodes/seed-cache/fixture.sql';self.path.parent.mkdir(parents=True);self.path.write_bytes(self.raw)
        self.build={'build_fingerprint':'build-fixture'};self.build_path=self.root/'evidence/mainline/email_build.json';save(self.build_path,self.build)
        self.record={'schema_version':'vip-isolated-runtime-seed-v1','task_commit':PIN,'task_id':TASK_ID,
            'schemas':['wc','gsheet','email'],'sheets_and_email_rows_included':False,
            'task_config_sha256':PUBLIC_SHA256[BASE+'task_config.json'],'email_build_fingerprint':self.build['build_fingerprint'],
            'email_build_record_sha256':hashlib.sha256(self.build_path.read_bytes()).hexdigest(),'training_sample':False,
            'readonly_sha256':'a'*64,'seed_ref':self.path.relative_to(self.root).as_posix(),
            'sha256':hashlib.sha256(self.raw).hexdigest(),'bytes':len(self.raw)}
        self.verify=patch('integrations.email_build.verify_build');self.verify.start();self.addCleanup(self.verify.stop)

    def test_valid_owned_sql_returns_exact_record_and_path(self):
        save(self.root/SEED_REF,self.record)
        self.assertEqual(validated_seed(self.root),(self.record,self.path))

    def test_foreign_task_config_build_and_data_scope_are_refused(self):
        for key,value in (('task_id','other'),('task_commit','0'*40),('schemas',['wc','email']),
            ('sheets_and_email_rows_included',True),('training_sample',True),('task_config_sha256','0'*64),
            ('email_build_fingerprint','foreign'),('email_build_record_sha256','0'*64),('readonly_sha256',None)):
            with self.subTest(field=key),self.assertRaises(ResourceError):validated_seed(self.root,{**self.record,key:value})

    def test_rehashed_writable_sql_and_changed_size_are_refused(self):
        for table in ('email.messages','gsheet.cells'):
            raw=self.raw+('COPY '+table+' FROM stdin;\n').encode();self.path.write_bytes(raw)
            changed={**self.record,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
            with self.assertRaises(ResourceError):validated_seed(self.root,changed)
        self.path.write_bytes(self.raw)
        with self.assertRaises(ResourceError):validated_seed(self.root,{**self.record,'bytes':True})

    def test_changed_seed_bytes_or_build_receipt_cannot_be_reused(self):
        self.path.write_bytes(self.raw+b'changed')
        with self.assertRaises(ResourceError):validated_seed(self.root,self.record)
        self.path.write_bytes(self.raw);save(self.build_path,{**self.build,'changed':True})
        with self.assertRaises(ResourceError):validated_seed(self.root,self.record)

    def test_seed_path_outside_immutable_cache_is_refused(self):
        path=self.root/'foreign.sql';path.write_bytes(self.raw)
        with self.assertRaises(ResourceError):validated_seed(self.root,{**self.record,'seed_ref':'foreign.sql'})


class VipBridgeHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def dispatch(self,adapter,body):
        try:from integrations.toolathlon_bridge import Resources
        except ImportError:self.skipTest('Bridge requires original-task optional dependencies')
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        resource=object.__new__(Resources);resource.adapter=adapter;resource.ledger=Mock()
        sid=body['resources_session_id'];state={'run':Path(temp.name),'requests':{},'trace':[]}
        resource.get=lambda value:(sid,state)
        with patch('integrations.toolathlon_bridge.native.save'):
            result=await resource._dispatch('/tool',body)
        return result,resource,state

    def body(self):return {'resources_session_id':str(uuid4()),'episode_id':{'rollout_id':'fixture','attempt':0},
        'request_id':str(uuid4()),'server':'email','name':'send_email','arguments':{'body':'fixture'}}

    async def test_explicit_request_interface_receives_exact_outer_uuid_and_cache(self):
        body=self.body();adapter=SimpleNamespace(call_request=AsyncMock(return_value={'sent':True}),call=AsyncMock())
        result,resource,state=await self.dispatch(adapter,body)
        again=await resource._dispatch('/tool',body)
        self.assertEqual(result,again);adapter.call.assert_not_awaited()
        adapter.call_request.assert_awaited_once_with('email','send_email',body['arguments'],body['request_id'],state['trace'])

    async def test_legacy_adapter_keeps_original_signature(self):
        body=self.body();adapter=SimpleNamespace(call=AsyncMock(return_value={'legacy':True}))
        result,_,state=await self.dispatch(adapter,body)
        self.assertEqual(result,{'result':{'legacy':True}})
        adapter.call.assert_awaited_once_with('email','send_email',body['arguments'],state['trace'])

    async def test_type_error_after_dispatch_does_not_fall_back_or_retry(self):
        adapter=SimpleNamespace(call_request=AsyncMock(side_effect=TypeError('after possible write')),call=AsyncMock())
        with self.assertRaises(TypeError):await self.dispatch(adapter,self.body())
        adapter.call_request.assert_awaited_once();adapter.call.assert_not_awaited()


if __name__=='__main__':unittest.main()
