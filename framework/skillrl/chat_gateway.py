"""Lossless upstream Chat recording and native Messages protocol adaptation."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

from .storage import atomic
from .vendor.a2o import convert_request_a2o, StreamingA2OConverter, build_tool_name_mapping


def deepseek_request(incoming):
    converted = convert_request_a2o(incoming)
    fields = ('model', 'messages', 'tools', 'tool_choice', 'max_tokens', 'stop')
    payload = {key: converted[key] for key in fields if key in converted}
    for message in payload['messages']:
        if message['role'] == 'system' and isinstance(message['content'], list):
            message['content'] = '\n'.join(p['text'] for p in message['content'] if p.get('type') == 'text')
        message.pop('reasoning', None)
        message.pop('thinking_blocks', None)
    payload.update(thinking={'type': 'enabled'}, reasoning_effort='high', stream=True,
                   stream_options={'include_usage': True}, top_p=1.0)
    return payload


class ChatStream:
    def __init__(self, model, names):
        self.converter = StreamingA2OConverter(model, names)
        self.finish_reason = None
        self.usage = None
        self.done = False
        self.error = None
        self.reasoning = ''
        self.text = ''
        self.message_id = None
        self.reported_model = None

    def feed(self, chunk):
        if chunk.get('error'):
            self.error = chunk['error']
            return []
        self.message_id = chunk.get('id', self.message_id)
        self.reported_model = chunk.get('model', self.reported_model)
        if isinstance(chunk.get('usage'), dict):
            self.usage = chunk['usage']
        for choice in chunk.get('choices', []):
            delta = choice.get('delta', {})
            self.reasoning += delta.get('reasoning_content') or delta.get('reasoning') or ''
            self.text += delta.get('content') or ''
            if choice.get('finish_reason') is not None:
                self.finish_reason = choice['finish_reason']
        return self.converter.feed(chunk)

    @property
    def complete(self):
        return self.done and self.finish_reason is not None and self.error is None


class ChatMessagesGateway:
    def __init__(self, base_url, key, model, path, *, timeout=900):
        self.base_url = base_url.rstrip('/')
        self.key = key
        self.model = model
        self.path = Path(path)
        self.timeout = timeout
        self.lock = threading.Lock()
        self.counter = 0
        self.identities = {}
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                started = time.monotonic()
                with gateway.lock:
                    index = gateway.counter
                    gateway.counter += 1
                path = gateway.path / f'{index:04d}'
                path.mkdir(parents=True)
                incoming_raw = self.rfile.read(int(self.headers['Content-Length']))
                (path / 'request.messages.raw').write_bytes(incoming_raw)
                identity = hashlib.sha256(incoming_raw).hexdigest()
                atomic(path / 'intent.json', {'time': time.time(), 'incoming_sha256': identity})
                state = ChatStream(gateway.model, {})
                response = None
                forwarded = False
                status = None
                headers_seconds = None
                first_delta = last_delta = first_byte = None
                generated_chunks = set()
                native_started = False
                response_started = False
                client_gone = False
                bytes_read = chunk_count = 0
                failure = None

                def send(data):
                    nonlocal client_gone
                    with (path / 'response.anthropic.sse').open('ab') as saved:
                        saved.write(data)
                    if not client_gone:
                        try:
                            self.wfile.write(data)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            client_gone = True

                def accept(chunk):
                    nonlocal native_started, first_delta, last_delta
                    if chunk.get('error'):
                        state.feed(chunk)
                        return
                    if not chunk.get('choices') and not chunk.get('usage'):
                        return
                    active = any((c.get('delta', {}).get('reasoning_content') or c.get('delta', {}).get('reasoning') or c.get('delta', {}).get('content') or c.get('delta', {}).get('tool_calls')) for c in chunk.get('choices', []))
                    if active:
                        now = time.monotonic() - started
                        first_delta = now if first_delta is None else first_delta
                        last_delta = now
                        generated_chunks.add(chunk_count)
                    if not native_started:
                        state.converter.model = chunk.get('model', gateway.model)
                        send(state.converter.start())
                        native_started = True
                    for event in state.feed(chunk):
                        send(event)

                try:
                    incoming = json.loads(incoming_raw)
                    payload = deepseek_request(incoming)
                    if payload['model'] != gateway.model:
                        raise ValueError('Unexpected Chat model')
                    state = ChatStream(gateway.model, build_tool_name_mapping(incoming.get('tools')))
                    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
                    atomic(path / 'request.json', payload)
                    (path / 'request.raw').write_bytes(raw)
                    with gateway.lock:
                        previous = gateway.identities.get(identity)
                        if previous and previous not in ('http_rejection', 'upstream_error'):
                            raise ValueError('Prior identical request requires reconciliation')
                        gateway.identities[identity] = 'pending'
                    url = gateway.base_url + '/chat/completions'
                    atomic(path / 'forward.json', {'time': time.time(), 'url': url, 'request_sha256': hashlib.sha256(raw).hexdigest()})
                    forwarded = True
                    headers = {'Content-Type': 'application/json'}
                    if gateway.key:
                        headers['Authorization'] = 'Bearer ' + gateway.key
                    try:
                        response = urllib.request.urlopen(urllib.request.Request(url, data=raw, headers=headers), timeout=gateway.timeout)
                    except urllib.error.HTTPError as error:
                        response = error
                    status = response.status
                    headers_seconds = time.monotonic() - started
                    public = {k:v for k,v in response.headers.items() if k.lower() in ('content-type','request-id','x-request-id','retry-after','server','via','x-upstream-model','x-model')}
                    atomic(path / 'headers.json', {'status': status, 'headers': public})
                    self.send_response(status)
                    self.send_header('Content-Type', 'text/event-stream' if status == 200 else 'application/json')
                    if response.headers.get('Retry-After'):
                        self.send_header('Retry-After', response.headers['Retry-After'])
                    self.end_headers()
                    response_started = True
                    if status != 200:
                        body = response.read()
                        (path / 'response.raw').write_bytes(body)
                        send(json.dumps({'type':'error','error':{'type':'api_error','message':body.decode(errors='replace')}}).encode())
                    else:
                        pending = b''
                        is_json = 'application/json' in response.headers.get('Content-Type','')
                        with (path / 'response.raw').open('wb') as saved, (path / 'chunk-arrivals.jsonl').open('w', buffering=1) as arrivals:
                            while True:
                                data = response.read1(65536)
                                if not data:
                                    break
                                chunk_count += 1
                                elapsed = time.monotonic() - started
                                first_byte = elapsed if first_byte is None else first_byte
                                saved.write(data)
                                saved.flush()
                                arrivals.write(json.dumps({'chunk':chunk_count,'seconds':elapsed,'offset':bytes_read,'bytes':len(data)})+'\n')
                                bytes_read += len(data)
                                pending += data
                                if not is_json:
                                    pending = pending.replace(b'\r\n', b'\n')
                                    while b'\n\n' in pending:
                                        frame, pending = pending.split(b'\n\n', 1)
                                        for line in frame.splitlines():
                                            if not line.startswith(b'data:'):
                                                continue
                                            value = line[5:].strip()
                                            if value == b'[DONE]':
                                                state.done = True
                                            elif value:
                                                accept(json.loads(value))
                                if elapsed > gateway.timeout:
                                    raise TimeoutError('Upstream recording deadline reached')
                        if is_json:
                            body = json.loads(pending)
                            if body.get('error'):
                                state.error = body['error']
                            else:
                                choices = []
                                for choice in body.get('choices', []):
                                    delta = dict(choice.get('message', {}))
                                    if delta.get('tool_calls'):
                                        delta['tool_calls'] = [dict(call, index=i) for i,call in enumerate(delta['tool_calls'])]
                                    choices.append({'delta':delta,'finish_reason':choice.get('finish_reason')})
                                accept({'id':body.get('id'),'model':body.get('model'),'choices':choices,'usage':body.get('usage')})
                                state.done = True
                        if state.complete:
                            if not native_started:
                                send(state.converter.start())
                            for event in state.converter.finish():
                                send(event)
                            atomic(path / 'response.anthropic.json', state.converter.build_final_response())
                        else:
                            message = str(state.error or 'Incomplete upstream stream; inspect receipt')
                            send(('event: error\ndata: '+json.dumps({'type':'error','error':{'type':'api_error','message':message}})+'\n\n').encode())
                except Exception as error:
                    failure = {'kind':type(error).__name__,'message':str(error)}
                    atomic(path / 'failure.json', failure)
                    if not response_started:
                        self.send_response(502 if forwarded else 409)
                        self.send_header('Content-Type','application/json')
                        self.end_headers()
                    send(json.dumps({'type':'error','error':{'type':'api_error','message':str(error)}}).encode())
                finally:
                    if response is not None:
                        response.close()
                    disposition = 'complete' if state.complete else 'http_rejection' if status and status >= 400 else 'upstream_error' if state.error else 'unknown'
                    with gateway.lock:
                        gateway.identities[identity] = disposition
                    (path / 'reasoning.txt').write_text(state.reasoning)
                    (path / 'content.txt').write_text(state.text)
                    seconds = time.monotonic() - started
                    tokens = (state.usage or {}).get('completion_tokens') if state.complete else None
                    span = last_delta-first_delta if first_delta is not None and last_delta is not None else None
                    usable = isinstance(tokens,int) and tokens>1 and span is not None and span>=.1 and len(generated_chunks)>=2
                    atomic(path / 'receipt.json', {'complete':state.complete,'disposition':disposition,'forwarded':forwarded,'http_status':status,'reported_model':state.reported_model,'message_id':state.message_id,'finish_reason':state.finish_reason,'done':state.done,'usage':state.usage if state.complete else None,'usage_missing':state.usage is None,'reasoning_characters':len(state.reasoning),'text_characters':len(state.text),'seconds':seconds,'failure':failure,'client_disconnected':client_gone,'anthropic_usage_placeholder':state.usage is None})
                    atomic(path / 'timing.json', {'request_seconds':seconds,'headers_seconds':headers_seconds,'first_byte_seconds':first_byte,'first_output_delta_seconds':first_delta,'last_output_delta_seconds':last_delta,'observed_output_span_seconds':span,'end_to_end_output_tokens_per_second':tokens/seconds if isinstance(tokens,int) and seconds>0 else None,'observed_stream_output_tokens_per_second':tokens/span if usable else None,'stream_rate_usable':usable,'socket_chunks':chunk_count,'output_delivery_chunks':len(generated_chunks)})

        self.server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.key = ''
