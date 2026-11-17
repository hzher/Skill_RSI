"""Standalone native hook recording context transitions without editing history."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time


def snapshot(event, root):
    destination = Path(root) / (str(time.time_ns()) + '-' + event['hook_event_name'])
    destination.mkdir(parents=True)
    record = {'time_ns': time.time_ns(), 'event': event, 'transcript': None, 'notes': {}}
    transcript = Path(event.get('transcript_path', '/nonexistent'))
    if transcript.is_file():
        data = transcript.read_bytes()
        (destination / 'transcript.jsonl').write_bytes(data)
        record['transcript'] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    notes = Path(os.environ.get('SKILLRL_NOTES_DIR', '/workspace/notes'))
    if notes.exists():
        for path in sorted(notes.rglob('*')):
            if path.is_file() and not path.is_symlink():
                name = str(path.relative_to(notes))
                target = destination / 'notes' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                record['notes'][name] = {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    (destination / 'event.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    return record


if __name__ == '__main__':
    snapshot(json.load(sys.stdin), os.environ['SKILLRL_CONTEXT_EVENTS'])
    print('{}', flush=True)
