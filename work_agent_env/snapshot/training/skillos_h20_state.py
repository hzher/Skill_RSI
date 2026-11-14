"""Durable native update/install transitions. No model or device imports."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path

from integrations.benchmark_tasks.contracts import digest, strict_json


def read(path):
    return strict_json(Path(path).read_text())


def file_ref(path):
    path = Path(path).absolute()
    if path.resolve() != path or not path.is_file() or path.is_symlink():
        raise ValueError('Expected plain canonical artifact: ' + str(path))
    before = path.stat()
    with path.open('rb') as stream:
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('Artifact changed during hashing')
    return {'path': str(path), 'sha256': sha}


def checked(reference):
    if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'} or file_ref(reference['path']) != reference:
        raise ValueError('Artifact reference changed')
    return read(reference['path'])


def atomic(path, value, *, new=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.resolve() != path or (new and path.exists()):
        raise ValueError('Artifact path is linked or already exists: ' + str(path))
    temp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    with temp.open('xb') as stream:
        stream.write((json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class State:
    NEXT = {'actor': ('PREPARED', 'ACTOR_SENT', 'ACTOR_SAVED'),
            'validate': ('ACTOR_SAVED', 'VALIDATE_SENT', 'VALIDATED'),
            'export': ('VALIDATED', 'EXPORT_SENT', 'EXPORTED'),
            'install': ('EXPORTED', 'INSTALL_SENT', 'INSTALLED')}

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'lifecycle.json'

    @contextmanager
    def lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / '.lifecycle.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def create(self, context_ref):
        with self.lock():
            atomic(self.path, {'phase': 'PREPARED', 'context': context_ref, 'events': [], 'receipts': {}}, new=True)

    def get(self):
        value = read(self.path)
        checked(value['context'])
        return value

    def begin(self, stage, *, command):
        with self.lock():
            value = self.get()
            before, sent, _ = self.NEXT[stage]
            if value['phase'] != before:
                raise ValueError('Cannot replay/start ' + stage + ' from ' + value['phase'])
            value['phase'] = sent
            value['events'].append({'stage': stage, 'event': 'sent', 'command': command,
                                    'at': datetime.now(timezone.utc).isoformat()})
            atomic(self.path, value)

    def finish(self, stage, receipt_ref):
        with self.lock():
            value = self.get()
            _, sent, completed = self.NEXT[stage]
            if value['phase'] != sent:
                raise ValueError('Invalid stage completion')
            checked(receipt_ref)
            value['phase'] = completed
            value['receipts'][stage] = receipt_ref
            value['events'].append({'stage': stage, 'event': 'completed', 'receipt': receipt_ref})
            atomic(self.path, value)

    def unknown(self, stage, reason):
        with self.lock():
            value = self.get()
            allowed = {self.NEXT[stage][1]}
            if stage == 'install':
                allowed.add('INSTALLED')
            if value['phase'] not in allowed:
                raise ValueError('Invalid uncertain stage transition')
            value['phase'] = stage.upper() + '_UNKNOWN'
            value['events'].append({'stage': stage, 'event': 'unknown', 'reason': reason})
            atomic(self.path, value)


def update_uuid(run_id, step, batch_sha256):
    from integrations.skill_generator.skillos.upstream import _module, SOURCE, verify_sources
    verify_sources()
    original = _module(SOURCE + 'mainline_contract.py', names={'canonical', 'update_uuid'},
                       namespace={'json': json, 'hashlib': hashlib})
    return original.update_uuid(run_id, step, batch_sha256)
