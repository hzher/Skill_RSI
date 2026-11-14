"""Check privacy transforms and archive safety without touching experiment data."""
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from export import archive_write, sanitize
from verify import extract_member, safe_path, scan


class ExportChecks(unittest.TestCase):
    def test_json_paths_only_and_native_fields_preserved(self):
        original = {'cwd': '/home/local-account/work', 'task_id': 'public-owner/task:7',
                    'reward': 0.75, 'tool': {'id': 'call_123', 'result': 'actual returned value'},
                    'windows': r'C:\Users\local-account\project',
                    'workspace': r'E:\research\local-project\memoraX\2026.11.11'}
        data, changes = sanitize(json.dumps(original).encode(), 'fixture.json')
        clean = json.loads(data)
        self.assertEqual(clean['cwd'], '/home/operator/work')
        self.assertEqual(clean['windows'], r'C:\Users\operator\project')
        self.assertEqual(clean['workspace'], '/workspace\\2026.11.11')
        for key in ['task_id', 'reward', 'tool']:
            self.assertEqual(clean[key], original[key])
        self.assertTrue(changes)

    def test_unchanged_bytes_and_bom_preserved(self):
        data = b'\xef\xbb\xbf{"task":"public:1","native_success":false}\r\n'
        self.assertEqual(sanitize(data, 'fixture.json'), (data, {}))

    def test_mixed_wsl_stderr_keeps_both_parts(self):
        prefix = 'wsl: localhost warning\r\n'.encode('utf-16-le')
        suffix = b'failure at /home/local-account/project\n'
        clean, changes = sanitize(prefix + suffix, 'stderr.log')
        self.assertEqual(clean, prefix + b'failure at /home/operator/project\n')
        scan(clean, 'stderr.log')

    def test_nested_gzip_normalized_and_scanned(self):
        original = gzip.compress(b'/home/local-account/session\n', mtime=42)
        clean, changes = sanitize(original, 'fixture.gz')
        self.assertEqual(gzip.decompress(clean), b'/home/operator/session\n')
        self.assertTrue(changes)
        secret = ('sk-' + 'A'*40).encode()
        with self.assertRaisesRegex(ValueError, 'Credential-shaped'):
            sanitize(gzip.compress(secret), 'fixture.gz')
        with self.assertRaisesRegex(ValueError, 'Credential-shaped'):
            scan(gzip.compress(secret), 'fixture.gz')

    def test_archive_paths_reject_traversal(self):
        with zipfile.ZipFile(io.BytesIO(), 'w') as archive:
            for name in ['../escape', '/escape', 'a/../../escape', 'a\\escape']:
                with self.assertRaises(ValueError):
                    archive_write(archive, name, b'x')
        for name in ['../escape', '/escape', 'C:/escape', 'a\\escape']:
            with self.assertRaises(ValueError):
                safe_path(Path.cwd(), name)

    def test_unknown_binary_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'Unreviewed binary'):
            sanitize(b'\xff\x81\x90', 'unexpected.bin')

    def test_extract_refuses_overwriting_different_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = extract_member(root, 'nested/evidence.json', b'original')
            extract_member(root, 'nested/evidence.json', b'original')
            with self.assertRaisesRegex(ValueError, 'Refuse to overwrite'):
                extract_member(root, 'nested/evidence.json', b'different')
            self.assertEqual(path.read_bytes(), b'original')


if __name__ == '__main__':
    unittest.main()
