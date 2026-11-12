"""Opt-in direct transport; retry before exposing any model output to tools."""
import hashlib
import http.client
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
from .storage import atomic


class IncompleteStream(ConnectionError):
    pass


class TemporaryProviderError(ConnectionError):
    pass


def buffered_request(request, path, *, timeout, options, secret='', responses=False):
    """Keep physical receipts; never replay a completed model response or tool action."""
    root = Path(path) / 'transport-attempts'
    attempts = int(options.get('max_attempts', 8))
    if not 1 <= attempts <= 30:
        raise ValueError('Transport max_attempts outside 1..30')
    deadline = time.monotonic() + min(float(options.get('max_elapsed', 480)), timeout)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    for index in range(attempts):
        target = root / f'{index:04d}'
        target.mkdir(parents=True, exist_ok=False)
        atomic(target / 'request.json', {'url': request.full_url, 'method': request.get_method(),
            'body_sha256': hashlib.sha256(request.data or b'').hexdigest(),
            'route': 'direct; environment proxies disabled', 'ordinal': index})
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Transport elapsed budget exhausted')
            with opener.open(request, timeout=min(float(options.get('attempt_timeout', 120)), remaining)) as response:
                # Read completely before returning anything to native Codex. A failed
                # response therefore cannot execute a partial set of tool calls.
                with (target / 'response.raw').open('wb') as stream:
                    size = 0
                    while chunk := response.read(65536):
                        size += len(chunk)
                        if size > 32 * 1024 * 1024:
                            raise ValueError('Provider response exceeds byte budget')
                        stream.write(chunk)
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Transport elapsed budget exhausted')
                content_type = response.headers.get('Content-Type', 'application/json')
                raw = (target / 'response.raw').read_bytes()
                if responses:
                    events = []
                    for line in raw.decode(errors='replace').splitlines():
                        if line.startswith('data: '):
                            try:
                                events.append(json.loads(line[6:]))
                            except ValueError:
                                pass
                    for event in events:
                        if event.get('type') not in ('response.failed', 'response.incomplete'):
                            continue
                        code = (event.get('response', {}).get('error') or {}).get('code')
                        if code in ('upstream_error', 'server_error', 'rate_limit_exceeded', 'temporarily_unavailable'):
                            raise TemporaryProviderError('Provider temporary SSE error: ' + code)
                        raise ValueError('Provider returned a terminal unsuccessful response')
                    if not any(e.get('type') == 'response.completed' for e in events):
                        raise IncompleteStream('SSE ended without response.completed')
                atomic(target / 'result.json', {'status': response.status, 'bytes': len(raw),
                    'response_sha256': hashlib.sha256(raw).hexdigest(), 'elapsed': time.monotonic() - started})
                atomic(Path(path) / 'transport-summary.json', {'physical_attempts': index + 1,
                    'recovered_errors': index, 'selected_attempt': index, 'route': 'direct',
                    'prior_attempt_costs_unknown': bool(index), 'buffered_before_delivery': True})
                return raw, content_type
        except Exception as error:
            status = error.code if isinstance(error, urllib.error.HTTPError) else None
            transient = status in (408, 429, 500, 502, 503, 504) if status else isinstance(
                error, (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead))
            message = str(error)
            if secret:
                message = message.replace(secret, '[REDACTED]')
            receipt = {'kind': type(error).__name__, 'message': message[:1200],
                       'status': status, 'transient': transient, 'attempt': index}
            atomic(target / 'failure.json', receipt)
            delay = min(float(options.get('initial_delay', 2)) * 2 ** index, 30)
            if not transient or index + 1 >= attempts or time.monotonic() + delay >= deadline:
                atomic(Path(path) / 'transport-exhausted.json', receipt)
                raise
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            time.sleep(delay)
