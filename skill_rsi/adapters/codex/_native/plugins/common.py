"""Shared pure helpers; no dependency on a plugin family."""
import hashlib
from ..storage import canonical

def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def pointer(value, path):
    if not isinstance(path, str) or (path and not path.startswith('/')):
        raise ValueError('Expected a JSON pointer')
    for token in path.split('/')[1:] if path else []:
        key = token.replace('~1', '/').replace('~0', '~')
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value
