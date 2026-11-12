import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from skillrl.transport import buffered_request
from skillrl.curation import validate
from skillrl.vendor.submit import schema


class Response(io.BytesIO):
    status = 200
    headers = {'Content-Type': 'text/event-stream'}


class TransportTests(unittest.TestCase):
    def test_curator_rejection_identifies_invalid_name_and_preserves_noop_contract(self):
        skill = {'name': 'invalid_name', 'description': 'Description', 'applicability': 'Condition',
                 'exclusions': 'Exclusions', 'steps': ['First step', 'Second step'],
                 'validation': ['Check result'], 'dependencies': []}
        value = {'reason': 'Evidence reason', 'operations': [{'operation': 'add', 'parents': [],
                 'skills': [skill], 'evidence_ids': ['e00001'], 'reason': 'Evidence reason'}]}
        with self.assertRaisesRegex(ValueError, r'skills\[0\].name: must match'):
            validate(value, schema(enforce_shapes=True))
        validate({'reason': 'No justified change', 'operations': []}, schema(enforce_shapes=True))

    def call(self, folder, outcomes, **kwargs):
        with patch('skillrl.transport.urllib.request.build_opener') as factory:
            factory.return_value.open.side_effect = outcomes
            return buffered_request(urllib.request.Request('https://example.test/responses', data=b'{}'),
                Path(folder), timeout=20, options={'max_attempts': 3, 'initial_delay': 0}, **kwargs)

    def test_disconnect_receipt_preserved_and_only_complete_stream_returned(self):
        raw = b'data: {"type":"response.completed","response":{}}\n\n'
        with tempfile.TemporaryDirectory() as folder:
            body, _ = self.call(folder, [urllib.error.URLError('refused'), Response(raw)], responses=True)
            self.assertEqual(body, raw)
            root = Path(folder)
            self.assertTrue((root / 'transport-attempts/0000/failure.json').exists())
            self.assertEqual(json.loads((root / 'transport-summary.json').read_text())['recovered_errors'], 1)

    def test_partial_stream_is_retained_and_never_delivered(self):
        partial = b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
        complete = b'data: {"type":"response.completed","response":{}}\n\n'
        with tempfile.TemporaryDirectory() as folder:
            body, _ = self.call(folder, [Response(partial), Response(complete)], responses=True)
            self.assertEqual(body, complete)
            self.assertEqual((Path(folder) / 'transport-attempts/0000/response.raw').read_bytes(), partial)

    def test_authorization_error_is_not_retried(self):
        error = urllib.error.HTTPError('https://example.test', 401, 'bad auth', {}, io.BytesIO(b'bad'))
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(urllib.error.HTTPError):
                self.call(folder, [error])
            self.assertFalse((Path(folder) / 'transport-attempts/0001').exists())
            self.assertFalse(json.loads((Path(folder) / 'transport-exhausted.json').read_text())['transient'])

    def test_terminal_model_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                self.call(folder, [Response(b'data: {"type":"response.failed"}\n\n')], responses=True)
            self.assertFalse((Path(folder) / 'transport-attempts/0001').exists())

    def test_explicit_upstream_error_in_http_200_sse_is_retried(self):
        failed = b'data: {"type":"response.failed","response":{"error":{"code":"upstream_error"}}}\n\n'
        complete = b'data: {"type":"response.completed","response":{}}\n\n'
        with tempfile.TemporaryDirectory() as folder:
            body, _ = self.call(folder, [Response(failed), Response(complete)], responses=True)
            self.assertEqual(body, complete)
            self.assertTrue(json.loads((Path(folder) / 'transport-attempts/0000/failure.json').read_text())['transient'])
