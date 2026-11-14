"""Verify every exported raw member, readable episode, score and join offline."""
import argparse
from collections import Counter, defaultdict
import csv
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

from export import PACK, COUNTS, check_secrets, dump, fs, sha


def safe_path(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts or '\\' in relative or ':' in relative:
        raise ValueError('Unsafe relative path: ' + relative)
    target = fs(root / relative)
    if not target.resolve().is_relative_to(fs(root).resolve()):
        raise ValueError('Path escapes output root')
    return target


def scan(data, where):
    if data.startswith(b'w\0s\0l\0:\0'):
        boundary = data.find(b'\r\0\n\0')
        if boundary >= 0 and boundary + 4 < len(data):
            scan(data[:boundary+4], where + '!wsl-warning')
            scan(data[boundary+4:], where + '!stderr')
            return
    if data.startswith(b'\x1f\x8b'):
        scan(gzip.decompress(data), where + '!gzip')
    else:
        check_secrets(data, where)
        if data.startswith((b'\xff\xfe', b'\xfe\xff')):
            check_secrets(data.decode('utf-16').encode('utf-8'), where)
        elif data and len(data) % 2 == 0 and data.count(b'\0') / len(data) > 0.1:
            check_secrets(data.decode('utf-16-le').encode('utf-8'), where)


def extract_member(root, relative, data):
    target = safe_path(root, relative)
    if target.exists() and target.read_bytes() != data:
        raise ValueError('Refuse to overwrite different extracted file: ' + relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def verify(extract=None):
    manifest = json.loads((PACK/'SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
    results = json.loads((PACK/'results.json').read_text(encoding='utf-8'))
    archives = defaultdict(list)
    loose = []
    errors = []
    checked = 0
    exported_bytes = 0
    archive_sizes = {}
    for row in manifest['files']:
        (archives[row['archive']] if 'archive' in row else loose).append(row)
    def check(data, row, where):
        nonlocal checked, exported_bytes
        if sha(data) != row['exported_sha256'] or len(data) != row['exported_bytes']:
            errors.append('exported_hash:' + where)
        scan(data, where)
        checked += 1
        exported_bytes += len(data)
    for name, entries in archives.items():
        path = safe_path(PACK, name)
        archive_sizes[name] = path.stat().st_size
        if path.stat().st_size >= 50_000_000:
            errors.append('archive_exceeds_50MB:' + name)
        with zipfile.ZipFile(path) as archive:
            expected = [r['member'] for r in entries]
            if len(expected) != len(set(expected)) or set(archive.namelist()) != set(expected):
                errors.append('archive_members:' + name)
            for row in entries:
                data = archive.read(row['member'])
                check(data, row, name + '!' + row['member'])
                if extract:
                    extract_member(extract, row['member'], data)
    for row in loose:
        check(safe_path(PACK, row['file']).read_bytes(), row, row['file'])
    with (PACK/'reports/all-episodes.csv').open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    unique = defaultdict(set)
    passed = Counter()
    counts = Counter()
    kinds = Counter()
    new_turns, replay_turns = 0, 0
    auxiliary = 0
    seen = set()
    raw_members = {row['member']: row for entries in archives.values() for row in entries}
    for row in rows:
        identity = (row['run_id'], row['phase'], row['instance_id'])
        if identity in seen:
            errors.append('duplicate_episode:' + str(identity))
        seen.add(identity)
        path = safe_path(PACK, row['episode_path'])
        data = path.read_bytes()
        episode = json.loads(data.decode('utf-8-sig'))
        outcome = episode['native_outcome']
        if sha(data) != row['exported_episode_sha256']:
            errors.append('index_episode_hash:' + row['episode_path'])
        if raw_members[row['episode_path']]['exported_sha256'] != sha(data):
            errors.append('readable_raw_episode_disagreement:' + row['episode_path'])
        if any(episode[k] != row[k] for k in ['run_id','task','phase','instance_id']):
            errors.append('episode_identity:' + row['episode_path'])
        if outcome['instance_id'] != row['instance_id'] or outcome['success'] != (row['native_success'] == 'True') or float(outcome['reward']) != float(row['native_reward']):
            errors.append('native_outcome:' + row['episode_path'])
        interactions = episode['interactions']
        if len(interactions) != int(row['completed_interactions']) or len(interactions) != episode['completed_interactions']:
            errors.append('turn_count:' + row['episode_path'])
        kinds[row['evidence_kind']] += 1
        replay = episode.get('replayed_prefix_interactions', 0)
        replay_turns += replay
        new_turns += len(interactions) - replay
        if interactions:
            for turn in interactions:
                if not all(k in turn for k in ('query','response','observation')):
                    errors.append('incomplete_turn:' + row['episode_path'])
            # Original prompt digest can differ only when its host paths were normalized.
            prompt_hash = sha(interactions[0]['query']['prompt'].encode())
            if prompt_hash != episode['visible_initial_prompt_sha256']:
                record = next(r for r in loose if r['file'] == row['episode_path'])
                if not record['normalizations']:
                    errors.append('prompt_hash:' + row['episode_path'])
        else:
            actions = (episode.get('environment_evidence') or {}).get('native_hand_history',{}).get('actions',[])
            if row['task'] != 'exploitable_poker' or not actions or actions[-1]['action'] != 'FOLD' or any(a['player'] != 'opponent' for a in actions):
                errors.append('auto_resolution_evidence:' + row['episode_path'])
        trace = f"runs/collections/{row['run_id']}/" + episode['source_trace']
        if trace not in raw_members:
            errors.append('missing_native_trace:' + trace)
        for capture in episode.get('full_submission_evidence', {}).get('matching_captures', []):
            captured = f"runs/collections/{row['run_id']}/" + capture['path']
            if captured not in raw_members or raw_members[captured]['source_sha256'] != capture['sha256']:
                errors.append('missing_original_full_submission:' + captured)
        if row['role'] == 'primary':
            unique[row['task']].add(row['instance_id'])
            key = row['task'], row['phase']
            counts[key] += 1
            passed[key] += outcome['success'] is True
        else:
            auxiliary += 1
    for task, expected in COUNTS.items():
        if len(unique[task]) != expected:
            errors.append('coverage:' + task)
    for group in results['groups']:
        key = group['task'], group['phase']
        if counts[key] != group['records'] or passed[key] != group['native_success_true'] or passed[key]/counts[key] != group['native_success_rate']:
            errors.append('aggregate_score:' + str(key))
    if sum(counts.values()) != 320 or len(rows) != 322 or auxiliary != 2 or new_turns != 991 or replay_turns != 2:
        errors.append('global_count')
    if dict(kinds) != results['evidence_kinds_all_indexed']:
        errors.append('evidence_kinds')
    # Scan and inventory every published byte, not just original evidence entries.
    files = []
    for path in sorted(PACK.rglob('*')):
        if path.is_file() and not any(p in {'__pycache__', 'extracted', '.runtime'} for p in path.relative_to(PACK).parts):
            if path.name in {'VERIFICATION.json', 'PACKAGE_CHECKSUMS.json'}:
                continue
            data = path.read_bytes()
            if path.suffix != '.zip':
                scan(data, path.relative_to(PACK).as_posix())
            files.append({'path': path.relative_to(PACK).as_posix(), 'bytes': len(data), 'sha256': sha(data)})
    report = {'status': 'passed' if not errors else 'failed',
              'exported_evidence_entries_verified': checked,
              'exported_evidence_bytes_verified': exported_bytes,
              'raw_archives_verified': len(archives), 'archive_sizes_bytes': archive_sizes,
              'published_files_scanned': len(files), 'published_bytes': sum(f['bytes'] for f in files),
              'source_integrity': manifest['source_integrity'],
              'indexed_episodes_verified': len(rows), 'primary_records': sum(counts.values()),
              'auxiliary_records': auxiliary, 'unique_instances': sum(len(s) for s in unique.values()),
              'new_action_feedback_turns': new_turns, 'replayed_prefix_turns': replay_turns,
              'evidence_kinds': dict(kinds), 'errors': errors,
              'credential_shaped_findings': 0, 'model_calls': 0, 'benchmark_rerun': False,
              'original_source_modified': False}
    dump(PACK/'PACKAGE_CHECKSUMS.json', {'files': files, 'scope': 'All distributable files except this checksum file and VERIFICATION.json; Git additionally tracks those receipts.'})
    dump(PACK/'VERIFICATION.json', report)
    print(json.dumps(report, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extract', type=Path, help='Optional directory for reconstructing raw relative paths')
    args = parser.parse_args()
    verify(args.extract)
