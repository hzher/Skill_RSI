import contextlib
from copy import deepcopy
import io
import json
import os
import unittest
from unittest.mock import patch
import uuid

from integrations.skill_rsi_client import RSIClientError
from scripts.check_skill_rsi_handoff import check_candidate,check_handoff,load_request,load_token,main
from integrations.skill_rsi_contract import skill_version
from tests.test_skill_rsi import example_skill,request as evaluation_request
from tests import test_skill_rsi_client as clients
from tests import test_skill_rsi_catalog as catalogs


class HandoffTests(unittest.TestCase):
    setUp=clients.ClientTests.setUp
    runner=clients.ClientTests.runner
    service=clients.ClientTests.service
    start=clients.ClientTests.start

    def test_selected_catalog_and_capabilities_are_checked_without_submitting(self):
        catalogs.CatalogTests.retained(self)
        server,client=self.start();result=check_handoff(client,['unit','unit'])
        self.assertTrue(result['passed']);self.assertEqual(len(result['catalogs']),1)
        self.assertFalse(result['evaluation_submitted']);self.assertFalse(result['runtime_availability_attested'])
        with server.service.store.connect() as database:
            self.assertEqual(database.execute('SELECT count(*) FROM evaluations').fetchone()[0],0)
        self.assertEqual(self.calls,[])
        self.assertNotIn(clients.TOKEN,json.dumps(result))

    def test_missing_or_unknown_catalog_is_explicit_and_not_a_model_failure(self):
        _,client=self.start();result=check_handoff(client,['unit','DO_NOT_ECHO_UNKNOWN_INPUT'])
        self.assertFalse(result['passed'])
        self.assertEqual(result['catalogs'][0]['error'],'matching_tool_catalog_unavailable')
        self.assertEqual(result['catalogs'][1]['error'],'task_not_available')
        self.assertNotIn('DO_NOT_ECHO_UNKNOWN_INPUT',json.dumps(result))
        self.assertEqual(result['model_http_requests'],0);self.assertEqual(self.calls,[])

    def test_descriptor_content_must_match_advertised_executor(self):
        server,client=self.start();server.service.descriptor={**server.service.descriptor,'wall_seconds':999}
        with self.assertRaisesRegex(RSIClientError,'handoff_executor_descriptor_mismatch'):check_handoff(client)
        self.assertEqual(self.calls,[])

    def test_token_files_are_bounded_private_and_never_printed(self):
        path=self.root/'token';path.write_text(clients.TOKEN);os.chmod(path,0o600)
        self.assertEqual(load_token(path),clients.TOKEN)
        for raw in (b'x'*257,b'\xff'*40,(clients.TOKEN+'\x00').encode()):
            path.write_bytes(raw)
            with self.assertRaisesRegex(RSIClientError,'handoff_token_file_unavailable'):load_token(path)
        with self.assertRaises(RSIClientError):load_token(self.root)
        if os.name!='nt':
            path.write_text(clients.TOKEN);os.chmod(path,0o644)
            with self.assertRaises(RSIClientError):load_token(path)

    def test_cli_prints_metadata_on_success_and_fixed_errors_on_auth_failure(self):
        catalogs.CatalogTests.retained(self);server,_=self.start()
        path=self.root/'token';path.write_text(clients.TOKEN);os.chmod(path,0o600)
        args=['--token-file',str(path),'--base-url','http://127.0.0.1:'+str(server.server_address[1]),'--task','unit']
        output=io.StringIO()
        with contextlib.redirect_stdout(output):status=main(args)
        self.assertEqual(status,0);self.assertTrue(json.loads(output.getvalue())['passed'])
        path.write_text('wrong-local-token-with-sufficient-length')
        output=io.StringIO()
        with contextlib.redirect_stdout(output):status=main(args)
        self.assertEqual(status,2);self.assertEqual(json.loads(output.getvalue())['error'],'unauthorized')
        self.assertNotIn(clients.TOKEN,output.getvalue());self.assertEqual(self.calls,[])

    def candidate(self):
        return evaluation_request({**example_skill(),'allowed_tools':['unit__read']})

    def test_candidate_check_uses_retained_tools_without_creating_an_intent(self):
        catalogs.CatalogTests.retained(self);server,client=self.start();body=self.candidate()
        body['skill']['objective']='PRIVATE_CANDIDATE_OBJECTIVE'
        result=check_candidate(client,body)
        self.assertTrue(result['passed']);self.assertEqual(result['request_id'],body['request_id'])
        self.assertEqual(result['skill_sha256'],skill_version(body['skill']))
        self.assertTrue(result['tool_permissions_checked']);self.assertEqual(result['allowed_tool_count'],1)
        self.assertFalse(result['evaluation_submitted']);self.assertFalse(result['runtime_availability_attested'])
        self.assertFalse(result['business_success_attested']);self.assertFalse(result['training_eligible'])
        with server.service.store.connect() as database:
            self.assertEqual(database.execute('SELECT count(*) FROM evaluations').fetchone()[0],0)
        self.assertNotIn('PRIVATE_CANDIDATE_OBJECTIVE',json.dumps(result));self.assertEqual(self.calls,[])

    def test_unknown_tools_and_reference_writer_dependency_use_runtime_rules(self):
        catalogs.CatalogTests.retained(self);_,client=self.start();body=self.candidate()
        body['skill']['allowed_tools']=['PRIVATE_UNKNOWN_TOOL']
        result=check_candidate(client,body)
        self.assertFalse(result['passed']);self.assertEqual(result['error'],'skill_tool_not_available')
        self.assertEqual(result['unknown_tool_count'],1);self.assertNotIn('PRIVATE_UNKNOWN_TOOL',json.dumps(result))
        catalog=client.tool_catalog('unit')
        catalog['tools']=[{'name':'write_workbook'},{'name':'write_workbook_from_results'}]
        body['skill']['allowed_tools']=['write_workbook_from_results']
        with patch('integrations.skill_rsi_http.tool_catalog',return_value=catalog):
            result=check_candidate(client,body)
            self.assertEqual(result['error'],'reference_writer_requires_workbook_permission')
            body['skill']['allowed_tools'].append('write_workbook')
            self.assertTrue(check_candidate(client,body)['passed'])
        self.assertEqual(self.calls,[])

    def test_no_skill_check_does_not_claim_an_explicit_permission_set(self):
        catalogs.CatalogTests.retained(self);_,client=self.start()
        result=check_candidate(client,evaluation_request())
        self.assertTrue(result['passed']);self.assertEqual(result['skill_mode'],'no_skill_baseline')
        self.assertIsNone(result['skill_sha256']);self.assertIsNone(result['allowed_tool_count'])
        self.assertFalse(result['tool_permissions_checked']);self.assertEqual(self.calls,[])

    def test_invalid_candidate_is_rejected_before_any_network_request(self):
        class NoRPC:
            def capabilities(self):raise AssertionError('Network must not be called')
        for body in (None,{}, {**self.candidate(),'api_key':'PRIVATE_FIELD'},
                     {**self.candidate(),'request_id':'noncanonical'}):
            with self.subTest(kind=type(body).__name__),self.assertRaises(RSIClientError):
                check_candidate(NoRPC(),body)

    def test_candidate_input_is_frozen_before_the_capability_query(self):
        catalogs.CatalogTests.retained(self);_,client=self.start();body=self.candidate()
        original=deepcopy(body);capabilities=client.capabilities
        def mutate():
            body['request_id']=str(uuid.uuid4());body['skill']['objective']='PRIVATE_CHANGED_INPUT'
            return capabilities()
        with patch.object(client,'capabilities',side_effect=mutate):result=check_candidate(client,body)
        self.assertEqual(result['request_id'],original['request_id'])
        self.assertEqual(result['skill_sha256'],skill_version(original['skill']))
        self.assertNotIn('PRIVATE_CHANGED_INPUT',json.dumps(result))

    def test_wrong_environment_duplicate_tools_or_missing_catalog_are_explicit(self):
        _,client=self.start()
        with self.assertRaisesRegex(RSIClientError,'matching_tool_catalog_unavailable'):
            check_candidate(client,self.candidate())
        catalogs.CatalogTests.retained(self);catalog=client.tool_catalog('unit')
        wrong={**catalog,'environment_source_sha256':{'different':'environment'}}
        with patch('integrations.skill_rsi_http.tool_catalog',return_value=wrong):
            with self.assertRaisesRegex(RSIClientError,'handoff_catalog_binding_invalid'):
                check_candidate(client,self.candidate())
        wrong={**catalog,'tools':[{'name':'unit__read'},{'name':'unit__read'}]}
        with patch('integrations.skill_rsi_http.tool_catalog',return_value=wrong):
            with self.assertRaisesRegex(RSIClientError,'candidate_catalog_invalid'):
                check_candidate(client,self.candidate())
        with self.assertRaisesRegex(RSIClientError,'task_not_available'):
            check_candidate(client,{**self.candidate(),'task_id':'PRIVATE_UNKNOWN_TASK'})

    def test_private_candidate_files_and_cli_output_are_bounded_and_redacted(self):
        catalogs.CatalogTests.retained(self);server,_=self.start();body=self.candidate()
        token=self.root/'token';token.write_text(clients.TOKEN);os.chmod(token,0o600)
        path=self.root/'candidate';path.write_text(json.dumps(body));os.chmod(path,0o600)
        args=['--token-file',str(token),'--request-file',str(path),
              '--base-url','http://127.0.0.1:'+str(server.server_address[1])]
        output=io.StringIO()
        with contextlib.redirect_stdout(output):status=main(args)
        self.assertEqual(status,0);self.assertEqual(json.loads(output.getvalue())['schema_version'],'skill-rsi-candidate-check-v1')
        self.assertEqual(load_request(path),body);self.assertNotIn(clients.TOKEN,output.getvalue())
        for raw,code in ((b'x'*(128*1024+1),'candidate_request_file_unavailable'),
                         (b'\xff','candidate_request_json_invalid'),(b'{"a":1,"a":2}','candidate_request_json_invalid')):
            path.write_bytes(raw);output=io.StringIO()
            with contextlib.redirect_stdout(output):status=main(args)
            self.assertEqual(status,2);self.assertEqual(json.loads(output.getvalue())['error'],code)
        if os.name!='nt':
            path.write_text(json.dumps(body));os.chmod(path,0o644)
            with self.assertRaisesRegex(RSIClientError,'candidate_request_file_unavailable'):load_request(path)
