import http.client
import json
import threading
import unittest
import uuid
from urllib.parse import quote

from integrations.skill_rsi_http import SkillHTTPServer
from integrations.skill_rsi_contract import RSIError
from tests import test_skill_rsi as fixtures


class HTTPTests(unittest.TestCase):
    setUp = fixtures.SkillRSITests.setUp
    runner = fixtures.SkillRSITests.runner
    service = fixtures.SkillRSITests.service

    def start(self, service=None):
        server=SkillHTTPServer(service or self.service(),'fixture-token-is-long-enough-for-authentication',port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def close():
            server.shutdown();server.wait_worker();server.server_close();thread.join()
        self.addCleanup(close)
        return server

    def call(self, server, method, path, value=None, **headers):
        conn=http.client.HTTPConnection(*server.server_address,timeout=5)
        self.addCleanup(conn.close)
        defaults={'Authorization':'Bearer '+server.token,'Content-Type':'application/json'}
        defaults.update(headers)
        raw=value if isinstance(value,bytes) else json.dumps(value).encode() if value is not None else None
        conn.request(method,path,raw,defaults)
        response=conn.getresponse()
        return response.status,json.loads(response.read())

    def test_async_submit_status_retry_and_busy_are_bounded(self):
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        def held(*args,**kwargs):
            entered.set();release.wait(4);return self.runner(*args,**kwargs)
        server=self.start(self.service(runner=held));body=fixtures.request(fixtures.example_skill())
        status,job=self.call(server,'POST','/v1/evaluations',body)
        self.assertEqual(status,202);self.assertTrue(entered.wait(2))
        status,retry=self.call(server,'POST','/v1/evaluations',body)
        self.assertEqual(status,200);self.assertEqual(retry['request_id'],body['request_id'])
        status,busy=self.call(server,'POST','/v1/evaluations',fixtures.request())
        self.assertEqual(status,429);self.assertEqual(busy['error'],'executor_busy')
        changed={**body,'skill':None}
        self.assertEqual(self.call(server,'POST','/v1/evaluations',changed)[0],409)
        release.set();server.wait_worker()
        status,done=self.call(server,'GET','/v1/evaluations/'+body['request_id'])
        self.assertEqual(status,200);self.assertEqual(done['state'],'completed');self.assertEqual(len(self.calls),1)
        self.assertNotIn('objective',json.dumps(done));self.assertNotIn(server.token,json.dumps(done))

    def test_auth_origin_strict_json_size_and_configuration_are_checked_before_execution(self):
        server=self.start();body=fixtures.request()
        self.assertEqual(self.call(server,'POST','/v1/evaluations',body,Authorization='wrong')[0],401)
        self.assertEqual(self.call(server,'POST','/v1/evaluations',body,Origin='https://example.invalid')[0],403)
        self.assertEqual(self.call(server,'POST','/v1/evaluations',b'{"x":1,"x":2}')[0],400)
        self.assertEqual(self.call(server,'POST','/v1/evaluations',body,**{'Content-Length':'131073'})[0],413)
        self.assertEqual(self.call(server,'POST','/v1/evaluations',body,**{'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.calls,[])
        status,cap=self.call(server,'GET','/v1/capabilities')
        self.assertEqual(status,200);self.assertEqual(cap['skill_schema'],'skill-v1')
        self.assertNotIn('unit-secret',json.dumps(cap));self.assertNotIn('https://unit.invalid',json.dumps(cap))
        with self.assertRaises(RSIError):SkillHTTPServer(self.service(),'x'*40,host='0.0.0.0',port=0)

    def test_restart_exposes_orphan_intent_without_automatically_running_it(self):
        service=self.service();body=fixtures.request();service.prepare(body)
        server=self.start(self.service())
        status,job=self.call(server,'GET','/v1/evaluations/'+body['request_id'])
        self.assertEqual(status,200);self.assertEqual(job['state'],'accepted')
        self.assertEqual(self.call(server,'POST','/v1/evaluations',body)[1]['state'],'accepted')
        self.assertEqual(self.call(server,'POST','/v1/evaluations',fixtures.request())[0],409)
        self.assertEqual(self.calls,[])

    def test_registry_endpoint_only_returns_diagnostic_metadata(self):
        server=self.start();skill=fixtures.example_skill()
        body={'schema_version':'skill-rsi-registration-v1','request_id':str(uuid.uuid4()),'skill':skill,'expected_head':None}
        status,version=self.call(server,'POST','/v1/versions',body)
        self.assertEqual(status,200);self.assertEqual(version['namespace'],'diagnostic')
        self.assertNotIn(skill['objective'],json.dumps(version))
        self.assertEqual(self.call(server,'GET','/v1/versions/'+version['version']),(200,version))
        self.assertEqual(self.call(server,'GET','/v1/tool-catalogs/unit')[1]['error'],'matching_tool_catalog_unavailable')
        self.assertEqual(self.calls,[])

    def test_head_endpoint_allows_cas_re_read_without_exposing_skill_body(self):
        server=self.start();skill=fixtures.example_skill();skill['skill_id']='办公/报告 ?#'
        path='/v1/skill-heads/'+quote(skill['skill_id'],safe='')
        self.assertEqual(self.call(server,'GET',path,Authorization='wrong')[0],401)
        self.assertIsNone(self.call(server,'GET',path)[1]['version'])
        body={'schema_version':'skill-rsi-registration-v1','request_id':str(uuid.uuid4()),'skill':skill,'expected_head':None}
        first=self.call(server,'POST','/v1/versions',body)[1]['version']
        changed={**skill,'steps':skill['steps']+['Check changed inputs']}
        update={**body,'request_id':str(uuid.uuid4()),'skill':changed,'expected_head':first}
        second=self.call(server,'POST','/v1/versions',update)[1]['version']
        stale={**update,'request_id':str(uuid.uuid4())}
        self.assertEqual(self.call(server,'POST','/v1/versions',stale)[0],409)
        status,head=self.call(server,'GET',path)
        self.assertEqual(status,200);self.assertEqual(head['version'],second)
        self.assertEqual(head['skill_id'],skill['skill_id'])
        self.assertNotIn(skill['objective'],json.dumps(head))
        self.assertEqual(self.call(server,'GET','/v1/capabilities')[1]['skill_head_route'],'/v1/skill-heads/{skill_id}')
        self.assertEqual(self.calls,[])

    def test_head_endpoint_rejects_invalid_encoding_and_raw_path_delimiters(self):
        server=self.start()
        for component in ('','%ZZ','%FF','x?query=y','x#fragment','x/y','%20','x'*101):
            with self.subTest(component=component):
                status,result=self.call(server,'GET','/v1/skill-heads/'+component)
                self.assertEqual(status,400);self.assertEqual(result['error'],'invalid_skill_identity')
        self.assertEqual(self.calls,[])

    def test_executor_precondition_fails_before_creating_an_intent(self):
        server=self.start();body=fixtures.request()
        status,reply=self.call(server,'POST','/v1/evaluations',body,**{'If-Match':'"'+'a'*64+'"'})
        self.assertEqual((status,reply),(409,{'error':'executor_precondition_failed'}))
        with self.assertRaisesRegex(RSIError,'evaluation_not_found'):server.service.store.get(body['request_id'])
        self.assertEqual(self.calls,[])
        status,_=self.call(server,'POST','/v1/evaluations',body,**{'If-Match':'"'+server.service.executor_id+'"'})
        self.assertEqual(status,202);server.wait_worker();self.assertEqual(len(self.calls),1)

    def test_invalid_executor_condition_and_control_char_tokens_are_rejected(self):
        server=self.start()
        for condition in ('*','a'*64,'"short"','"'+'a'*64+'", "'+'b'*64+'"'):
            self.assertEqual(self.call(server,'POST','/v1/evaluations',fixtures.request(),**{'If-Match':condition}),
                (400,{'error':'invalid_executor_precondition'}))
        for token in ('a'*40+'\x00','a'*40+'\x7f'):
            with self.assertRaises(RSIError):SkillHTTPServer(self.service(),token,port=0)
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
