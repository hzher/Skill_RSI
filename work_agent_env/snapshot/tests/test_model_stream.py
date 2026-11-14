"""Native stream fixtures test terminal completion, tools, signatures and faults."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from dataclasses import replace

from integrations.model_endpoint import EndpointClient,EndpointError,EndpointSettings,provider_error_code,run_probe
from integrations.model_stream import read_message
from integrations.model_usage import ModelUsageLedger
from tests.test_model_endpoint import fixture,server,KEY


def frame(value):
    return ('event: '+value['type']+'\r\ndata: '+json.dumps(value,ensure_ascii=False)+'\r\n\r\n').encode()


def native_events(message):
    start={**message,'content':[],'stop_reason':None}
    rows=[{'type':'message_start','message':start}]
    for index,block in enumerate(message['content']):
        rows.append({'type':'content_block_start','index':index,'content_block':block})
        rows.append({'type':'content_block_stop','index':index})
    rows.extend([{'type':'message_delta','delta':{'stop_reason':message.get('stop_reason','end_turn')},'usage':message.get('usage',{})},
                 {'type':'message_stop'}])
    return b''.join(frame(value) for value in rows)


class Fragmented(io.BytesIO):
    def read1(self,size):return self.read(min(size,3))


class ModelStreamTests(unittest.TestCase):
    def test_incomplete_output_limit_has_fixed_category_without_returning_partial_tools(self):
        raw=frame({'type':'response.incomplete','response':{'id':'resp-unit','status':'incomplete',
            'incomplete_details':{'reason':'max_output_tokens'},'output':[{'type':'function_call','arguments':'PRIVATE_PARTIAL'}]}})
        with self.assertRaises(EndpointError) as caught:
            read_message(io.BytesIO(raw),'openai_responses',deadline=time.monotonic()+2)
        self.assertEqual(caught.exception.code,'incomplete_model_response')
        self.assertEqual(caught.exception.provider_error_code,'output_token_limit')
        self.assertNotIn('PRIVATE',str(caught.exception))

    def test_unknown_incomplete_reason_is_never_guessed_or_exported(self):
        raw=frame({'type':'response.incomplete','response':{'id':'resp-unit','status':'incomplete',
            'incomplete_details':{'reason':'PRIVATE_REASON'}}})
        with self.assertRaises(EndpointError) as caught:
            read_message(io.BytesIO(raw),'openai_responses',deadline=time.monotonic()+2)
        self.assertIsNone(caught.exception.provider_error_code)
    def test_responses_terminal_keeps_encrypted_reasoning_and_native_ids(self):
        message=fixture('openai_responses',1)
        raw=frame({'type':'response.created','response':{**message,'status':'in_progress'}})+frame({'type':'response.completed','response':message})
        self.assertEqual(read_message(Fragmented(raw),'openai_responses',deadline=time.monotonic()+3),message)

    def test_thinking_signature_and_tool_json_fragments_are_reassembled(self):
        events=[{'type':'message_start','message':{'id':'msg-unit','content':[],'usage':{'input_tokens':5}}},
                {'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':'','signature':''}},
                {'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'想法'}},
                {'type':'content_block_delta','index':0,'delta':{'type':'signature_delta','signature':'signed-opaque'}},
                {'type':'content_block_stop','index':0},
                {'type':'content_block_start','index':1,'content_block':{'type':'tool_use','id':'call-unit','name':'add','input':{}}},
                {'type':'content_block_delta','index':1,'delta':{'type':'input_json_delta','partial_json':'{"a":'}},
                {'type':'content_block_delta','index':1,'delta':{'type':'input_json_delta','partial_json':'2}'}},
                {'type':'content_block_stop','index':1},
                {'type':'message_delta','delta':{'stop_reason':'tool_use'},'usage':{'output_tokens':9}},{'type':'message_stop'}]
        result=read_message(Fragmented(b''.join(frame(e) for e in events)),'anthropic_messages',deadline=time.monotonic()+3)
        self.assertEqual(result['content'][0],{'type':'thinking','thinking':'想法','signature':'signed-opaque'})
        self.assertEqual(result['content'][1]['input'],{'a':2})
        self.assertEqual(result['usage'],{'input_tokens':5,'output_tokens':9})

    def test_partial_function_call_never_escapes_without_terminal_event(self):
        message=fixture('anthropic_messages',1)
        raw=native_events(message).rsplit(frame({'type':'message_stop'}),1)[0]
        with self.assertRaises(EndpointError) as caught:read_message(io.BytesIO(raw),'anthropic_messages',deadline=time.monotonic()+2)
        self.assertEqual(caught.exception.code,'stream_interrupted_outcome_unknown')

    def test_stream_error_keeps_only_a_fixed_provider_category(self):
        raw=frame({'type':'error','error':{'type':'overloaded_error','message':KEY}})
        with self.assertRaises(EndpointError) as caught:read_message(io.BytesIO(raw),'anthropic_messages',deadline=time.monotonic()+2)
        self.assertEqual(caught.exception.provider_error_code,'overloaded_error')
        self.assertNotIn(KEY,str(caught.exception))

    def test_only_terminal_response_identity_is_used_without_merging_deltas(self):
        message=fixture('openai_responses',0)
        changed={**message,'id':'other'}
        raw=frame({'type':'response.created','response':message})+frame({'type':'response.completed','response':changed})
        metadata={}
        self.assertEqual(read_message(io.BytesIO(raw),'openai_responses',deadline=time.monotonic()+2,metadata=metadata),changed)
        self.assertEqual(metadata,{'response_envelope_id_count':2})

    def test_ambiguous_streamed_tool_json_is_rejected(self):
        raw=frame({'type':'message_start','message':{'id':'msg-unit','content':[]}})+frame({'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'c','name':'t','input':{}}})
        raw+=frame({'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':'{"a":1,"a":2}'}})+frame({'type':'content_block_stop','index':0})
        with self.assertRaises(EndpointError):read_message(io.BytesIO(raw),'anthropic_messages',deadline=time.monotonic()+2)

    def test_additional_message_delta_metadata_is_not_assistant_content(self):
        message=fixture('anthropic_messages',0)
        raw=native_events(message).replace(b'"stop_reason": "end_turn"}',b'"stop_reason": "end_turn", "container": null}')
        result=read_message(io.BytesIO(raw),'anthropic_messages',deadline=time.monotonic()+2)
        self.assertEqual(result['content'],message['content'])
        self.assertNotIn('container',result)

    def test_deadline_and_wire_budgets_stop_incomplete_streams(self):
        for raw,deadline in ((b'data: '+b'x'*300+b'\n',time.monotonic()+2),(b'',time.monotonic()-1)):
            with self.assertRaises(EndpointError):read_message(io.BytesIO(raw),'openai_responses',deadline=deadline,max_response_bytes=64)

    def test_both_native_protocols_finish_three_step_sse_probe(self):
        for protocol in ('openai_responses','anthropic_messages'):
            replies=[]
            for phase in range(3):
                value=fixture(protocol,phase)
                raw=frame({'type':'response.completed','response':value}) if protocol=='openai_responses' else native_events(value)
                replies.append((200,raw,{'Content-Type':'text/event-stream'}))
            with self.subTest(protocol=protocol),tempfile.TemporaryDirectory() as folder,server(replies) as (base,requests):
                settings=EndpointSettings(protocol,base,'unit-model',KEY,max_tokens=256,timeout_seconds=2)
                result=run_probe(settings,Path(folder)/'probe',stream=True)
                self.assertEqual(result['status'],'passed')
                self.assertTrue(all(r['body']['stream'] for r in requests))
                self.assertTrue(all(r['response_format']=='sse' for r in result['responses']))

    def test_http_error_diagnostics_never_store_the_error_body_or_secret(self):
        with tempfile.TemporaryDirectory() as folder,server([(502,{'error':{'message':'maximum context length exceeded '+KEY}}, {})]) as (base,requests):
            client=EndpointClient(EndpointSettings('openai_responses',base,'unit',KEY),ModelUsageLedger(Path(folder)),diagnose_http_errors=True)
            with self.assertRaises(EndpointError) as caught:client.request({'model':'unit'})
            self.assertEqual(caught.exception.provider_error_code,'context_limit')
            self.assertEqual(client.attempts,1)
            self.assertNotIn(KEY,''.join(p.read_text() for p in Path(folder).glob('*.json')))

    def test_provider_codes_and_text_are_not_arbitrary_public_metadata(self):
        self.assertIsNone(provider_error_code({'error':{'code':KEY,'message':KEY}}))
        self.assertIsNone(provider_error_code({'error':{'code':{}}}))
        self.assertEqual(provider_error_code({'error':{'message':'upstream request timed out'}}),'provider_timeout')


if __name__=='__main__':unittest.main()
