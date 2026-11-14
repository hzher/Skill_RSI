"""Bounded SSE assembly; no tool call escapes before a terminal model event."""
from copy import deepcopy
import json
import time


def read_message(response,protocol,*,deadline,max_response_bytes=1024*1024,metadata=None):
    # Imported lazily by the endpoint client, avoiding an import cycle.
    from integrations.model_endpoint import EndpointError,strict_json,provider_error_code
    if protocol not in ('openai_responses','anthropic_messages'):raise EndpointError('unsupported_stream_protocol')
    buffer=b'';data=[];event_name=None;wire_bytes=0;event_count=0
    message=None;blocks={};pending=set();fragments={};response_ids=set()

    def event(value,name):
        nonlocal message
        if not isinstance(value,dict):raise EndpointError('invalid_stream_event')
        kind=value.get('type')
        if not isinstance(kind,str):raise EndpointError('invalid_stream_event')
        if name and name!=kind:raise EndpointError('stream_event_type_mismatch')
        if kind in ('error','response.failed','response.incomplete'):
            body=value.get('response',value)
            category=provider_error_code(body)
            if (kind=='response.incomplete' and isinstance(body,dict)
                and isinstance(body.get('incomplete_details'),dict)
                and body['incomplete_details'].get('reason')=='max_output_tokens'):
                category='output_token_limit'
            raise EndpointError('provider_stream_error' if kind!='response.incomplete' else 'incomplete_model_response',
                                http_status=200,provider_error_code=category)
        if protocol=='openai_responses':
            if kind in ('response.created','response.in_progress','response.completed'):
                body=value.get('response')
                if not isinstance(body,dict) or not isinstance(body.get('id'),str):raise EndpointError('invalid_stream_response')
                if len(body['id'])>500:raise EndpointError('invalid_stream_response')
                response_ids.add(body['id'])
                if kind=='response.completed':
                    if body.get('status')!='completed':raise EndpointError('incomplete_model_response')
                    # Some routers rewrite provisional envelope IDs. We never
                    # assemble their output deltas: only this complete terminal
                    # response is authoritative, including its IDs and content.
                    if metadata is not None:metadata['response_envelope_id_count']=len(response_ids)
                    return body
            return None
        if kind=='ping':return None
        if kind=='message_start':
            if message is not None:raise EndpointError('duplicate_stream_start')
            message=value.get('message')
            if not isinstance(message,dict) or message.get('content')!=[]:raise EndpointError('invalid_stream_start')
            message=deepcopy(message)
            return None
        if message is None:raise EndpointError('missing_stream_start')
        if kind=='message_delta':
            delta=value.get('delta',{})
            if not isinstance(delta,dict):raise EndpointError('unsupported_stream_message_delta')
            # Additional server metadata is not assistant content. Preserve the
            # completion fields; tool/thinking/signature blocks remain exact.
            message.update({key:delta[key] for key in ('stop_reason','stop_sequence') if key in delta})
            if isinstance(value.get('usage'),dict):
                if not isinstance(message.get('usage',{}),dict):raise EndpointError('invalid_stream_usage')
                message.setdefault('usage',{}).update(value['usage'])
        elif kind=='message_stop':
            if pending or message.get('stop_reason') not in ('end_turn','tool_use'):
                raise EndpointError('incomplete_model_response')
            if set(blocks)!=set(range(len(blocks))):raise EndpointError('stream_block_inventory')
            message['content']=[blocks[index] for index in range(len(blocks))]
            return message
        elif kind.startswith('content_block_'):
            index=value.get('index')
            if type(index) is not int or not 0<=index<128:raise EndpointError('stream_block_inventory')
            if kind=='content_block_start':
                block=value.get('content_block')
                if index in blocks or not isinstance(block,dict) or block.get('type') not in ('text','thinking','redacted_thinking','tool_use'):
                    raise EndpointError('unsupported_stream_block')
                blocks[index]=deepcopy(block);pending.add(index);fragments[index]=''
            elif kind=='content_block_delta':
                if index not in pending:raise EndpointError('stream_delta_without_block')
                delta=value.get('delta',{});mode=delta.get('type');block=blocks[index]
                if mode=='input_json_delta' and block['type']=='tool_use':
                    piece=delta.get('partial_json')
                    if not isinstance(piece,str):raise EndpointError('invalid_stream_delta')
                    fragments[index]+=piece
                else:
                    mapping={'text_delta':('text','text'),'thinking_delta':('thinking','thinking'),'signature_delta':('thinking','signature')}
                    if mode not in mapping:raise EndpointError('unsupported_stream_delta')
                    expected,key=mapping[mode];piece=delta.get(key)
                    if block['type']!=expected or not isinstance(piece,str):raise EndpointError('invalid_stream_delta')
                    block[key]=block.get(key,'')+piece
            elif kind=='content_block_stop':
                if index not in pending:raise EndpointError('stream_stop_without_block')
                if blocks[index]['type']=='tool_use' and fragments[index]:
                    parsed=strict_json(fragments[index])
                    if not isinstance(parsed,dict):raise EndpointError('invalid_tool_arguments')
                    blocks[index]['input']=parsed
                pending.remove(index)
            else:raise EndpointError('unsupported_stream_event')
        else:raise EndpointError('unsupported_stream_event')
        return None

    while True:
        if time.monotonic()>=deadline:raise EndpointError('timeout_outcome_unknown')
        chunk=response.read1(65536)
        if not chunk:raise EndpointError('stream_interrupted_outcome_unknown')
        wire_bytes+=len(chunk)
        if wire_bytes>8*max_response_bytes:raise EndpointError('stream_wire_budget_exhausted')
        buffer+=chunk
        while b'\n' in buffer:
            line,buffer=buffer.split(b'\n',1);line=line.rstrip(b'\r')
            if len(line)>max_response_bytes:raise EndpointError('stream_frame_budget_exhausted')
            if not line:
                if data:
                    event_count+=1
                    if event_count>50000:raise EndpointError('stream_event_budget_exhausted')
                    raw=b'\n'.join(data)
                    if raw==b'[DONE]':raise EndpointError('stream_interrupted_outcome_unknown')
                    result=event(strict_json(raw),event_name)
                    if result is not None:
                        if len(json.dumps(result,allow_nan=False).encode())>max_response_bytes:raise EndpointError('response_too_large')
                        return result
                data=[];event_name=None
                continue
            if line.startswith(b':'):continue
            field,_,part=line.partition(b':')
            if part.startswith(b' '):part=part[1:]
            if field==b'data':data.append(part)
            elif field==b'event':
                try:event_name=part.decode('ascii')
                except UnicodeError:raise EndpointError('invalid_stream_event') from None
            if sum(map(len,data))>max_response_bytes:raise EndpointError('stream_frame_budget_exhausted')
        if len(buffer)>max_response_bytes:raise EndpointError('stream_frame_budget_exhausted')
