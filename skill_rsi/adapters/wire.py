"""Read complete Anthropic JSON or SSE responses without rewriting their bytes."""

# Adapted from Skill_RSI_yangyang/framework/skillrl/wire.py (MIT).
import json


def decode_response(body, status):
    events = []
    message = None
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and parsed.get('type') == 'message':
            message = parsed
        elif isinstance(parsed, dict) and parsed.get('type') == 'error':
            events.append(parsed)
    except (ValueError, UnicodeError):
        for line in body.splitlines():
            if line.startswith(b'data: '):
                try:
                    value = json.loads(line[6:])
                    if isinstance(value, dict):
                        events.append(value)
                except (ValueError, UnicodeError):
                    pass
    starts = [event['message'] for event in events if event.get('type') == 'message_start']
    deltas = [event for event in events if event.get('type') == 'message_delta']
    errors = [event.get('error') for event in events if event.get('type') == 'error']
    json_complete = message is not None and message.get('stop_reason') is not None and isinstance(message.get('usage'), dict)
    sse_complete = bool(starts) and bool(deltas) and deltas[-1].get('delta', {}).get('stop_reason') is not None and isinstance(deltas[-1].get('usage'), dict) and any(event.get('type') == 'message_stop' for event in events)
    complete = status == 200 and (json_complete or sse_complete)
    no_generated_output = not starts and message is None and not any(event.get('type') == 'content_block_delta' for event in events)
    explicit_upstream_error = bool(errors) and no_generated_output
    initial = message if message is not None else starts[0] if starts else {}
    final = message if message is not None else deltas[-1] if deltas else {}
    stop = message if message is not None else final.get('delta', {})
    usage = {**(initial.get('usage') or {}), **(final.get('usage') or {})} if complete else None
    return {'complete': complete, 'response_format': 'json' if message is not None else 'sse',
            'reported_model': initial.get('model'), 'message_id': initial.get('id'),
            'initial_usage': initial.get('usage'), 'final_usage': final.get('usage'),
            'usage': usage,
            'stop_reason': stop.get('stop_reason'), 'stop_details': stop.get('stop_details'),
            'explicit_upstream_error': explicit_upstream_error, 'upstream_errors': errors,
            'disposition': 'complete' if complete else 'gateway_error' if status >= 500 or explicit_upstream_error else 'http_rejection' if status >= 400 else 'unknown'}


class StreamTiming:
    """Measure delivery times without treating SSE frames as individual tokens."""
    def __init__(self, started):
        self.started = started
        self.pending = b''
        self.chunks = []
        self.events = []
        self.offset = 0
        self.output_times = []
        self.message_start = None
        self.models = set()

    def feed(self, chunk, now):
        elapsed = now - self.started
        self.chunks.append({'elapsed_seconds': elapsed, 'offset': self.offset, 'bytes': len(chunk)})
        self.offset += len(chunk)
        self.pending += chunk
        while b'\n\n' in self.pending or b'\r\n\r\n' in self.pending:
            delimiter = min((value for value in (b'\r\n\r\n', b'\n\n') if value in self.pending), key=self.pending.index)
            frame, self.pending = self.pending.split(delimiter, 1)
            data = b'\n'.join(line[5:].lstrip() for line in frame.splitlines() if line.startswith(b'data:'))
            if not data:
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            kind = event.get('type')
            row = {'elapsed_seconds': elapsed, 'type': kind, 'chunk_index': len(self.chunks)-1}
            if kind == 'message_start':
                self.message_start = elapsed
                model = event.get('message', {}).get('model')
                if model:
                    self.models.add(model)
            if kind == 'content_block_delta':
                delta = event.get('delta', {})
                row['delta_type'] = delta.get('type')
                value = delta.get('text', delta.get('thinking', delta.get('partial_json', '')))
                row['visible_characters'] = len(value)
                if value:
                    self.output_times.append((elapsed, len(self.chunks)-1))
            self.events.append(row)

    def summary(self, output_tokens, total_seconds, headers_seconds):
        first = self.output_times[0][0] if self.output_times else None
        last = self.output_times[-1][0] if self.output_times else None
        distinct_chunks = len({x[1] for x in self.output_times})
        span = last-first if first is not None and last is not None else None
        usable = isinstance(output_tokens, int) and output_tokens > 1
        rate_usable = usable and span is not None and span >= .1 and distinct_chunks >= 2
        return {'reported_output_tokens': output_tokens, 'headers_seconds': headers_seconds,
                'first_byte_seconds': self.chunks[0]['elapsed_seconds'] if self.chunks else None,
                'first_output_delta_seconds': first, 'last_output_delta_seconds': last,
                'observed_output_span_seconds': span, 'request_seconds': total_seconds,
                'end_to_end_output_tokens_per_second': output_tokens/total_seconds if usable and total_seconds > 0 else None,
                'observed_stream_output_tokens_per_second': output_tokens/span if rate_usable else None,
                'stream_rate_usable': rate_usable, 'output_delivery_chunks': distinct_chunks,
                'socket_read_chunks': len(self.chunks), 'sse_events': len(self.events),
                'reported_models': sorted(self.models),
                'rate_definition': 'Provider-reported output_tokens divided by observed delivery span. End-to-end includes request wait. Stream span is first to last nonempty text/thinking/tool-JSON delta. Frames are not tokens. Buffering and hidden output can affect these estimates.'}
