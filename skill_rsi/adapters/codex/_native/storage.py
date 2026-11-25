"""Durable artifacts, containment checks and one active writer per run."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import hashlib
import json
import os
import re
import shutil
from .vendor.common import atomic, canonical, file_sha, immutable, load

class PendingStage(RuntimeError):
    """A previous side effect has no durable completion receipt. Reconcile it first."""

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', value):
        raise ValueError('Invalid stable identifier')
    return value

def contained(root, name):
    root = Path(root).resolve()
    path = Path(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Expected a relative path inside the workspace')
    candidate = root / path
    current = candidate
    while current != root:
        if current.is_symlink():
            raise ValueError('Workspace symlinks require a benchmark-specific handler')
        current = current.parent
    if not candidate.resolve().is_relative_to(root):
        raise ValueError('Path outside the workspace')
    return candidate

def copy_public(source, destination):
    source, destination = Path(source).resolve(), Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for item in sorted(source.rglob('*')):
        if item.is_symlink() or not (item.is_dir() or item.is_file()):
            raise ValueError('Public input must contain ordinary files and directories')
        target = destination / item.relative_to(source)
        if item.is_dir():
            target.mkdir(exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)

def files(root):
    root = Path(root)
    result = {}
    for item in sorted(root.rglob('*')):
        if item.is_symlink():
            raise ValueError('Symlink in saved artifacts')
        if item.is_file():
            result[str(item.relative_to(root))] = {'sha256': file_sha(item), 'bytes': item.stat().st_size}
    return result

@contextmanager
def writer(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'OWNER.lock').open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PendingStage('Another writer owns this run') from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

def begin(path, request):
    path = Path(path)
    if (path / 'result.json').exists():
        if load(path / 'intent.json') != request:
            raise ValueError('Completed stage was called with different inputs')
        return load(path / 'result.json')
    if (path / 'intent.json').exists():
        raise PendingStage('Unresolved stage at ' + str(path))
    path.mkdir(parents=True, exist_ok=True)
    immutable(path / 'intent.json', canonical(request))
    return None

def seal(path, value):
    immutable(Path(path) / 'result.json', canonical(value))
    return value
