"""Export only explicitly selected original WorkBuddy public task archives."""
from pathlib import Path
import argparse
import hashlib
import json
import tarfile
from .storage import contained, identifier, atomic, files, file_sha


def export(dataset, selection, destination, *, strip_prefix=''):
    """Selection entries have id/path; graders and task-private files stay at source."""
    dataset, destination = Path(dataset).resolve(), Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    rows, provenance = [], []
    selected = set()
    for entry in selection:
        if set(entry) != {'id', 'path'}:
            raise ValueError('Selection only contains original id and relative source path')
        task_id = identifier(entry['id'])
        if task_id in selected:
            raise ValueError('Duplicate selected WorkBuddy task ID')
        selected.add(task_id)
        source = contained(dataset, entry['path'])
        instruction = source / 'instruction.md'
        archive = source / 'environment/workspace.tar.gz'
        target = destination / task_id
        target.mkdir()
        byte_count = 0
        with tarfile.open(archive, 'r:gz') as package:
            for member in package:
                if not (member.isfile() or member.isdir()) or member.size > 100_000_000:
                    raise ValueError('Workspace export rejects links, devices and oversized members')
                name = member.name
                if strip_prefix:
                    prefix = strip_prefix.rstrip('/') + '/'
                    if name.rstrip('/') == strip_prefix.rstrip('/'):
                        continue
                    if not name.startswith(prefix):
                        raise ValueError('Archive member outside explicitly chosen prefix')
                    name = name[len(prefix):]
                out = contained(target, name)
                if member.isdir():
                    out.mkdir(parents=True, exist_ok=True)
                else:
                    byte_count += member.size
                    if byte_count > 1_000_000_000:
                        raise ValueError('Task archive exceeds export budget')
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with package.extractfile(member) as stream, out.open('xb') as handle:
                        while chunk := stream.read(1024 * 1024):
                            handle.write(chunk)
        rows.append({'id': task_id, 'instruction': instruction.read_bytes().decode('utf-8'), 'public_dir': task_id})
        provenance.append({'id': task_id, 'instruction_sha256': file_sha(instruction),
                           'workspace_archive_sha256': file_sha(archive), 'public_files': files(target),
                           'explicit_strip_prefix': strip_prefix})
    if not rows:
        raise ValueError('Empty WorkBuddy selection')
    atomic(destination / 'tasks.json', rows)
    atomic(destination / 'provenance.json', provenance)
    return rows


def main():
    parser = argparse.ArgumentParser(description='Export selected original WorkBuddy public workspaces')
    parser.add_argument('--dataset', required=True, type=Path)
    parser.add_argument('--selection', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--strip-prefix', default='')
    args = parser.parse_args()
    rows = export(args.dataset, json.loads(args.selection.read_text()), args.out, strip_prefix=args.strip_prefix)
    print(json.dumps({'tasks': len(rows), 'manifest': str(args.out / 'tasks.json')}))

if __name__ == '__main__':
    main()
