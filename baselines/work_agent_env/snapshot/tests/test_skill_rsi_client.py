import http.server
import json
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from integrations.skill_rsi_client import MAX_REPLY_BYTES, RSIClientError, SkillRSIClient
from integrations.skill_rsi_contract import RSIError
from integrations.skill_rsi_http import SkillHandler,SkillHTTPServer
from integrations.skill_rsi_runtime import SkillRSI
from tests import test_skill_rsi as fixtures
from tests import test_skill_rsi_catalog as catalog_fixtures


TOKEN='client-fixture-token-with-at-least-thirty-two-characters'


class ClientTests(unittest.TestCase):
    setUp=fixtures.SkillRSITests.setUp
    runner=fixtures.SkillRSITests.runner
    service=fixtures.SkillRSITests.service

    def start(self, service=None):
        server=SkillHTTPServer(service or self.service(),TOKEN,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def stop():
            server.shutdown();server.wait_worker();server.server_close();thread.join()
        self.addCleanup(stop)
        return server,SkillRSIClient(TOKEN,base_url='http://127.0.0.1:'+str(server.server_address[1]))

    def stub(self, status, raw, *, content_type='application/json', disconnect=False, drip=False, drip_headers=False):
        requests=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.respond()
            def do_POST(self):self.respond()
            def respond(self):
                requests.append(self.command)
                if drip_headers:
                    header=(b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nX-Slow: '+b'x'*160
                            +b'\r\nContent-Length: '+str(len(raw)).encode()+b'\r\n\r\n')
                    try:
                        for byte in header:
                            self.wfile.write(bytes((byte,)));self.wfile.flush();time.sleep(.005)
                        self.wfile.write(raw)
                    except OSError:pass
                    return
                if disconnect:
                    if self.command=='GET':
                        value=json.dumps({'schema_version':'skill-rsi-capabilities-v1','executor_id':'a'*64,
                            'executor_precondition_supported':True}).encode()
                        self.send_response(200);self.send_header('Content-Type','application/json')
                        self.send_header('Content-Length',str(len(value)));self.end_headers();self.wfile.write(value)
                        return
                    self.close_connection=True
                    return
                self.send_response(status)
                self.send_header('Content-Type',content_type)
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                try:
                    if drip:
                        for byte in raw:
                            self.wfile.write(bytes((byte,)));self.wfile.flush();time.sleep(.005)
                    else:self.wfile.write(raw)
                except OSError:pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def stop():server.shutdown();server.server_close();thread.join()
        self.addCleanup(stop)
        return SkillRSIClient(TOKEN,base_url='http://127.0.0.1:'+str(server.server_address[1]),executor_id='a'*64),requests

    def test_submit_wait_retry_pin_and_catalog_use_existing_contracts(self):
        catalog_fixtures.CatalogTests.retained(self)
        server,client=self.start();request=fixtures.request(fixtures.example_skill())
        self.assertIsNone(client.executor_id)
        accepted=client.submit(request)
        self.assertEqual(client.executor_id,server.service.executor_id)
        self.assertIn(accepted['state'],('accepted','running','completed'))
        done=client.wait(request['request_id'],timeout_seconds=4,poll_seconds=.01)
        self.assertEqual(done['state'],'completed')
        self.assertEqual(client.submit(request),done)
        self.assertEqual(client.get_evaluation(request['request_id']),done)
        self.assertEqual(len(self.calls),1)
        self.assertIn('unit__read',{row['name'] for row in client.tool_catalog('unit')['tools']})

    def test_changed_executor_is_rejected_before_intent_creation(self):
        server,client=self.start();client.executor_id='a'*64;request=fixtures.request()
        with self.assertRaises(RSIClientError) as caught:client.submit(request)
        self.assertEqual(caught.exception.code,'executor_identity_changed')
        with self.assertRaisesRegex(RSIError,'evaluation_not_found'):server.service.store.get(request['request_id'])
        self.assertEqual(self.calls,[])
        with self.assertRaisesRegex(RSIClientError,'executor_identity_changed'):client.capabilities()

    def test_service_change_after_capability_read_is_rejected_atomically(self):
        server,client=self.start();client.capabilities()
        replacement=SkillRSI(self.root,self.settings,max_steps=99,runner=self.runner,preflight=lambda:None)
        self.assertNotEqual(replacement.executor_id,client.executor_id)
        server.service=replacement;body=fixtures.request()
        with self.assertRaises(RSIClientError) as caught:client.submit(body)
        self.assertEqual((caught.exception.code,caught.exception.http_status),('executor_precondition_failed',409))
        with self.assertRaisesRegex(RSIError,'evaluation_not_found'):replacement.store.get(body['request_id'])
        self.assertEqual(self.calls,[])

    def test_old_job_remains_queryable_but_cannot_implicitly_pin_a_new_submission(self):
        old=self.service().evaluate(fixtures.request())
        replacement=SkillRSI(self.root,self.settings,max_steps=99,runner=self.runner,preflight=lambda:None)
        _,client=self.start(replacement)
        self.assertEqual(client.get_evaluation(old['request_id']),old)
        with self.assertRaisesRegex(RSIClientError,'executor_identity_changed'):client.submit(fixtures.request())
        self.assertEqual(len(self.calls),1)

    def test_an_older_server_without_the_precondition_contract_gets_no_post(self):
        client,requests=self.stub(200,json.dumps({'schema_version':'skill-rsi-capabilities-v1','executor_id':'a'*64}).encode())
        with self.assertRaisesRegex(RSIClientError,'executor_precondition_unsupported'):client.submit(fixtures.request())
        self.assertEqual(requests,['GET'])

    def test_wait_timeout_leaves_the_running_worker_and_identity_intact(self):
        entered,release=threading.Event(),threading.Event()
        def held(*args,**kwargs):
            entered.set();release.wait(5);return self.runner(*args,**kwargs)
        server,client=self.start(self.service(runner=held));self.addCleanup(release.set)
        request=fixtures.request();client.submit(request);self.assertTrue(entered.wait(2))
        with self.assertRaisesRegex(RSIClientError,'evaluation_wait_timeout'):
            client.wait(request['request_id'],timeout_seconds=.08,poll_seconds=.01)
        self.assertEqual(server.service.store.get(request['request_id'])['state'],'running')
        release.set();server.wait_worker()
        self.assertEqual(client.wait(request['request_id'],timeout_seconds=2)['state'],'completed')
        self.assertEqual(len(self.calls),1)

    def test_wait_returns_an_orphan_or_unknown_without_posting_it(self):
        service=self.service();request=fixtures.request();service.prepare(request)
        service.store.transition(request['request_id'],'accepted','unknown',{'error':'fixture_interruption'})
        server,client=self.start(service)
        self.assertEqual(client.wait(request['request_id'],timeout_seconds=1)['state'],'unknown')
        self.assertEqual(self.calls,[])

    def test_version_cas_and_exact_unicode_head_identity_are_preserved(self):
        _,client=self.start();skill=fixtures.example_skill();skill['skill_id']=' 办公/候选 ?# '
        self.assertIsNone(client.current_head(skill['skill_id'])['version'])
        body={'schema_version':'skill-rsi-registration-v1','request_id':str(uuid.uuid4()),'skill':skill,'expected_head':None}
        first=client.register(body);self.assertEqual(client.version(first['version']),first)
        changed={**skill,'steps':skill['steps']+['Inspect the updated result']}
        update={**body,'request_id':str(uuid.uuid4()),'skill':changed,'expected_head':first['version']}
        second=client.register(update)
        with self.assertRaises(RSIClientError) as caught:client.register({**update,'request_id':str(uuid.uuid4())})
        self.assertEqual((caught.exception.code,caught.exception.http_status),('skill_version_conflict',409))
        self.assertEqual(client.current_head(skill['skill_id'])['version'],second['version'])
        self.assertEqual(client.current_head(skill['skill_id'])['skill_id'],skill['skill_id'])
        self.assertEqual(self.calls,[])

    def test_a_disconnected_post_is_sent_once_and_never_replayed(self):
        client,requests=self.stub(200,b'',disconnect=True)
        with self.assertRaisesRegex(RSIClientError,'submission_outcome_unknown'):client.submit(fixtures.request())
        self.assertEqual(requests,['GET','POST'])

    def test_untrusted_error_text_redirects_and_invalid_json_are_not_exposed(self):
        private='DO_NOT_ECHO_PRIVATE_BODY_OR_TOKEN'
        for status,body,kind in ((503,json.dumps({'error':private}).encode(),'remote_request_rejected'),
            (302,b'', 'redirect_not_allowed'),(200,b'{"state":1,"state":2}','invalid_response_json'),
            (200,b'not JSON '+private.encode(),'invalid_response_json')):
            client,requests=self.stub(status,body)
            with self.subTest(status=status,kind=kind),self.assertRaises(RSIClientError) as caught:
                client.get_evaluation(str(uuid.uuid4()))
            self.assertEqual(caught.exception.code,kind)
            self.assertNotIn(private,str(caught.exception));self.assertNotIn(TOKEN,repr(caught.exception))
            self.assertEqual(requests,['GET'])

    def test_response_budget_and_job_binding_fail_closed(self):
        client,_=self.stub(200,b' '*(MAX_REPLY_BYTES+1))
        with self.assertRaisesRegex(RSIClientError,'response_size_limit'):client.get_evaluation(str(uuid.uuid4()))
        identity=str(uuid.uuid4())
        for changed in ({'request_id':str(uuid.uuid4()),'state':'completed','executor_id':'a'*64},
            {'request_id':identity,'state':[],'executor_id':'a'*64},
            {'request_id':identity,'state':'completed','executor_id':'b'*64}):
            client,_=self.stub(200,json.dumps(changed).encode())
            with self.assertRaises(RSIClientError):client.get_evaluation(identity)
        client,_=self.stub(200,b'private body',content_type='text/html')
        with self.assertRaisesRegex(RSIClientError,'invalid_response_format'):client.get_evaluation(identity)

    def test_invalid_address_token_and_budgets_fail_without_a_connection(self):
        for address in ('https://127.0.0.1:18820','http://localhost:18820','http://127.0.0.1:18820/x',
                        'http://user@127.0.0.1:18820','http://127.0.0.1:70000'):
            with self.subTest(address=address),self.assertRaisesRegex(RSIClientError,'invalid_local_server_address'):
                SkillRSIClient(TOKEN,base_url=address)
        for token in ('short',TOKEN+'\r\n',TOKEN+'\x00',TOKEN+'\x7f'):
            with self.assertRaisesRegex(RSIClientError,'invalid_local_access_token'):SkillRSIClient(token)
        for budget in (True,0,-1,float('nan'),float('inf'),31,10**1000):
            with self.assertRaisesRegex(RSIClientError,'invalid_client_budget'):SkillRSIClient(TOKEN,timeout_seconds=budget)

    def test_continuously_dripping_body_cannot_extend_the_network_deadline(self):
        reply=json.dumps({'schema_version':'skill-rsi-capabilities-v1','executor_id':'a'*64,
                          'executor_precondition_supported':True}).encode()
        client,requests=self.stub(200,reply,drip=True);client.timeout_seconds=.08
        started=time.monotonic()
        with self.assertRaisesRegex(RSIClientError,'query_timeout'):client.capabilities()
        self.assertLess(time.monotonic()-started,.7)
        self.assertEqual(requests,['GET'])

    def test_wait_deadline_covers_slow_get_without_reposting_or_cancelling(self):
        identity=str(uuid.uuid4())
        reply=json.dumps({'request_id':identity,'executor_id':'a'*64,'state':'running','result':None}).encode()
        client,requests=self.stub(200,reply,drip=True)
        with self.assertRaisesRegex(RSIClientError,'evaluation_wait_timeout'):
            client.wait(identity,timeout_seconds=.08,poll_seconds=.01)
        self.assertEqual(requests,['GET'])

    def test_continuously_dripping_headers_cannot_extend_the_deadline(self):
        client,requests=self.stub(200,b'{}',drip_headers=True);client.timeout_seconds=.08
        started=time.monotonic()
        with self.assertRaisesRegex(RSIClientError,'query_timeout'):client.capabilities()
        self.assertLess(time.monotonic()-started,.7);self.assertEqual(requests,['GET'])

    def test_post_timeout_does_not_cancel_or_resubmit_an_accepted_worker(self):
        entered,release=threading.Event(),threading.Event()
        def held(*args,**kwargs):
            entered.set();release.wait(3);return self.runner(*args,**kwargs)
        server,client=self.start(self.service(runner=held));self.addCleanup(release.set)
        body=fixtures.request();client.capabilities();client.timeout_seconds=.08
        original=SkillHandler.reply
        def slow_reply(handler,*args,**kwargs):
            if handler.command=='POST':threading.Event().wait(.3)
            return original(handler,*args,**kwargs)
        with patch.object(SkillHandler,'reply',slow_reply):
            with self.assertRaisesRegex(RSIClientError,'submission_outcome_unknown'):client.submit(body)
            self.assertTrue(entered.wait(2));client.timeout_seconds=2
            self.assertEqual(client.get_evaluation(body['request_id'])['state'],'running')
            release.set();server.wait_worker()
            self.assertEqual(client.wait(body['request_id'],timeout_seconds=3)['state'],'completed')
        self.assertEqual(len(self.calls),1)

    def test_shorter_network_timeout_is_distinct_from_the_overall_wait_deadline(self):
        identity=str(uuid.uuid4())
        reply=json.dumps({'request_id':identity,'executor_id':'a'*64,'state':'running','result':None}).encode()
        client,requests=self.stub(200,reply,drip=True);client.timeout_seconds=.05
        with self.assertRaisesRegex(RSIClientError,'^query_timeout$'):
            client.wait(identity,timeout_seconds=2,poll_seconds=.01)
        self.assertEqual(requests,['GET'])

    def test_submission_snapshots_identity_and_skill_before_the_capability_roundtrip(self):
        server,client=self.start();body=fixtures.request(fixtures.example_skill())
        identity=body['request_id'];expected=json.loads(json.dumps(body['skill']))
        original=client.capabilities
        def changed_by_caller():
            result=original();body['request_id']=str(uuid.uuid4());body['skill']['steps'].append('Caller edited the candidate')
            return result
        client.capabilities=changed_by_caller
        job=client.submit(body)
        self.assertEqual(job['request_id'],identity)
        client.wait(identity,timeout_seconds=3,poll_seconds=.01)
        self.assertEqual(self.calls[0]['skill'],expected)
        with self.assertRaisesRegex(RSIError,'evaluation_not_found'):server.service.store.get(body['request_id'])

    def test_registration_snapshots_the_candidate_before_transport(self):
        _,client=self.start();skill=fixtures.example_skill()
        body={'schema_version':'skill-rsi-registration-v1','request_id':str(uuid.uuid4()),'skill':skill,'expected_head':None}
        expected=fixtures.skill_version(skill);original=client._request
        def changed_by_caller(*args,**kwargs):
            body['skill']['objective']='Caller edited during transport'
            return original(*args,**kwargs)
        client._request=changed_by_caller
        self.assertEqual(client.register(body)['version'],expected)

    def test_invalid_json_or_oversize_body_is_rejected_before_any_network_call(self):
        client,requests=self.stub(200,b'{}')
        circular=fixtures.request();circular['extra']=circular
        for body in ({**fixtures.request(),'extra':float('nan')},circular,
                     {**fixtures.request(),'extra':'x'*131073}):
            with self.assertRaises(RSIClientError):client.submit(body)
        self.assertEqual(requests,[])


if __name__=='__main__':unittest.main()
