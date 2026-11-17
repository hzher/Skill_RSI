import unittest
from skillrl.chat_gateway import deepseek_request, ChatStream

class ChatGatewayTests(unittest.TestCase):
    def test_high_thinking_and_history_preserved_for_both_endpoints(self):
        incoming={'model':'deepseek-v4-1-flash-261017','system':[{'type':'text','text':'native system','cache_control':{'type':'ephemeral'}}], 'messages':[{'role':'assistant','content':[{'type':'thinking','thinking':'original reasoning'},{'type':'tool_use','id':'t1','name':'mcp__benchmark__act','input':{'action':{'x':1}}}]},{'role':'user','content':[{'type':'tool_result','tool_use_id':'t1','content':'result'}]}], 'output_config':{'effort':'high'},'context_management':{'edits':[]},'max_tokens':32000,'stream':True}
        result=deepseek_request(incoming)
        self.assertEqual(result['messages'][0]['content'],'native system')
        self.assertEqual(result['messages'][1]['reasoning_content'],'original reasoning')
        self.assertEqual(result['thinking'],{'type':'enabled'})
        self.assertEqual(result['reasoning_effort'],'high')
        self.assertEqual(result['max_tokens'],32000)
        self.assertNotIn('context_management',result)
        self.assertNotIn('output_config',result)

    def test_complete_requires_actual_finish_and_done(self):
        state=ChatStream('m',{})
        state.feed({'id':'id1','model':'m','choices':[{'delta':{'reasoning_content':'think'},'finish_reason':None}]})
        self.assertFalse(state.complete)
        state.feed({'choices':[{'delta':{'content':'answer'},'finish_reason':'stop'}],'usage':{'prompt_tokens':4,'completion_tokens':9}})
        self.assertFalse(state.complete)
        state.done=True
        self.assertTrue(state.complete)
        self.assertEqual(state.reasoning,'think')
        self.assertEqual(state.usage['completion_tokens'],9)

    def test_no_usage_is_explicitly_missing(self):
        state=ChatStream('m',{})
        state.feed({'choices':[{'delta':{'content':'answer'},'finish_reason':'stop'}]});state.done=True
        self.assertTrue(state.complete)
        self.assertIsNone(state.usage)

class ChatGatewayHTTPTests(unittest.TestCase):
    def test_real_http_round_trip_saves_reasoning_and_original_stream(self):
        import json
        import tempfile
        import threading
        import urllib.request
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from pathlib import Path
        from skillrl.chat_gateway import ChatMessagesGateway
        events=[{'id':'chat1','model':'m','choices':[{'delta':{'reasoning_content':'actual thought'},'finish_reason':None}]},{'id':'chat1','model':'m','choices':[{'delta':{'content':'answer'},'finish_reason':'stop'}]},{'choices':[],'usage':{'prompt_tokens':4,'completion_tokens':7}}]
        raw=b''.join(b'data: '+json.dumps(e).encode()+b'\n\n' for e in events)+b'data: [DONE]\n\n'
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert payload['thinking']=={'type':'enabled'}
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=upstream.serve_forever,daemon=True);thread.start()
        with tempfile.TemporaryDirectory() as tmp:
            gateway=ChatMessagesGateway(f'http://127.0.0.1:{upstream.server_port}/v1','test-key','m',tmp)
            port=gateway.start()
            try:
                payload={'model':'m','system':'system','messages':[{'role':'user','content':'task'}],'max_tokens':32,'stream':True}
                request=urllib.request.Request(f'http://127.0.0.1:{port}/v1/messages',data=json.dumps(payload).encode())
                with urllib.request.urlopen(request) as response:body=response.read()
            finally:
                gateway.close();upstream.shutdown();upstream.server_close();thread.join()
            saved=Path(tmp)/'0000'
            self.assertEqual((saved/'response.raw').read_bytes(),raw)
            self.assertEqual((saved/'reasoning.txt').read_text(),'actual thought')
            self.assertTrue(json.loads((saved/'receipt.json').read_text())['complete'])
            self.assertIn(b'message_stop',body)
