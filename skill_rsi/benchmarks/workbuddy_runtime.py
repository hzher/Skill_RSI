"""Prepare required browser runtime libraries without changing task scoring."""
import hashlib
from pathlib import Path


def prepare_browser_runtime(task_root):
    """Patch private task copies; unlink first to protect hard-linked datasets."""
    changes = []
    for path in sorted(Path(task_root).glob('*/environment/Dockerfile')):
        before = path.read_bytes()
        text = before.decode()
        if 'python3 -m playwright install' not in text:
            continue
        marker = 'apt-get install -y --no-install-recommends'
        if marker not in text:
            raise ValueError('browser environment has no recognized dependency installation')
        installation = text.split(marker, 1)[1].split('&&', 1)[0]
        if 'libatomic1' in installation.split():
            continue
        after = text.replace(marker, marker + ' libatomic1', 1).encode()
        path.unlink()
        path.write_bytes(after)
        changes.append({'path': str(path), 'before_sha256': hashlib.sha256(before).hexdigest(),
                        'after_sha256': hashlib.sha256(after).hexdigest(),
                        'reason': 'Playwright Node runtime requires libatomic.so.1'})
    return changes
