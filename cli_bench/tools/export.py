"""Export completed benchmark evidence only; stdlib, read-only source, no inference.

The raw archives retain relative paths and all non-cache execution files.
Host account paths are normalized and embedded gzip streams are inspected too.
Original hashes remain evidence of the local originals, not exported-byte hashes.
"""
import argparse
from collections import Counter, defaultdict
import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import zipfile

PACK = Path(__file__).resolve().parents[1]
COUNTS = {'codebase_adaptation': 19, 'database_exploration': 40,
          'sales_prediction': 12, 'cohort_studies': 20,
          'blind_spectrum_monitoring': 90, 'exploitable_poker': 120}
PIN = 'a6f5ede32b4c6c13ccc4c793b3e9b9d7aebe5617'
REPORTS = {'collection-completion.json', 'collection-statistics.json',
           'continuity-verification.json', 'coverage-status.json',
           'raw-evidence-inventory.json', 'trajectory-inventory.md',
           'poker-auto-resolved-check.json', 'resume-counting-check.json',
           'runtime-incident-20261112.json', 'runtime-guard-check.json',
           'native-compaction-observation.json', 'codex-compaction-source-check.json',
           'native-resume-check.json', 'bridge-callback-check.json',
           'production-resource-limits-check.json', 'smoke-acceptance.json',
           'smoke-summary.json', 'credential-scan.json', 'collection-credential-scan.json'}
SCRIPTS = {'audit_collection.py', 'audit_smoke.py', 'clbench_bridge.py',
           'collect_suite.py', 'collection_progress.py', 'episode_inventory.py',
           'native_resume.py', 'raw_inventory.py', 'rsi.py', 'seal_native.py',
           'verify_actor_isolation.py', 'verify_collection.py', 'verify_continuity.py',
           'check_bridge_callbacks.py', 'check_native_resume.py'}
KEY = re.compile(rb'(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{24,}|(?:ghp|gho|github_pat|hf)_[A-Za-z0-9_]{30,})')
JWT = re.compile(rb'eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}')


def fs(path):
    path = Path(path).absolute()
    if os.name == 'nt' and not str(path).startswith('\\\\?\\'):
        return Path('\\\\?\\' + str(path))
    return path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(fs(path).read_text(encoding='utf-8-sig'))


def dump(path, value):
    fs(path).parent.mkdir(parents=True, exist_ok=True)
    fs(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def check_secrets(data, location):
    hits = [m.group() for m in KEY.finditer(data)]
    hits = [v for v in hits if not any(t in v.lower() for t in (b'example', b'placeholder', b'dummy', b'fake', b'test'))]
    if hits or JWT.search(data) or re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{40,}', data):
        raise ValueError('Credential-shaped content; inspect locally before export: ' + location)


def sanitize(data, location):
    # WSL can prepend a UTF-16LE warning to an otherwise UTF-8 stderr stream.
    if data.startswith(b'w\0s\0l\0:\0'):
        boundary = data.find(b'\r\0\n\0')
        if boundary >= 0 and boundary + 4 < len(data):
            prefix, first = sanitize(data[:boundary+4], location + '!wsl-warning')
            suffix, second = sanitize(data[boundary+4:], location + '!stderr')
            changes = Counter(first)
            changes.update(second)
            return prefix + suffix, dict(changes)
    if data.startswith(b'\x1f\x8b'):
        clean, changes = sanitize(gzip.decompress(data), location + '!gzip')
        return (gzip.compress(clean, mtime=0), changes) if changes else (data, {})
    check_secrets(data, location)
    encoding = 'utf-8-sig'
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        encoding = 'utf-16'
    elif data and len(data) % 2 == 0 and data.count(b'\0') / len(data) > 0.1:
        encoding = 'utf-16-le'
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        raise ValueError('Unreviewed binary evidence: ' + location) from None
    check_secrets(text.encode('utf-8'), location)
    changes = Counter()
    # Only host paths, never public repository owners, task IDs or session IDs.
    for label, pattern, replacement in [
        ('linux_home', r'/home/[^/\\\s"\'<>]+', '/home/operator'),
        ('windows_home', r'(?i)([A-Z]:)(\\{1,2}|/)Users\2[^/\\\s"\'<>]+', r'\1\2Users\2operator'),
        ('local_workspace', r'(?i)[A-Z]:(?:\\{1,2}|/)research(?:\\{1,2}|/)[^/\\\s"\'<>]+(?:\\{1,2}|/)memoraX', '/workspace'),
        ('wsl_mount_workspace', r'/mnt/[a-z]/research/[^/\\\s"\'<>]+/memoraX', '/workspace'),
    ]:
        text, count = re.subn(pattern, replacement, text)
        if count:
            changes[label] += count
    # Preserve bytes if no replacement, including BOM and original line endings.
    output_encoding = 'utf-8' if encoding == 'utf-8-sig' else encoding
    return (text.encode(output_encoding), dict(changes)) if changes else (data, {})


def approved_run_file(relative):
    parts = PurePosixPath(relative).parts
    if any(p in {'__pycache__', '.pytest_cache', '.git', 'node_modules', '.venv'} for p in parts):
        return False
    if PurePosixPath(relative).suffix.lower() in {'.pyc', '.pdf', '.doc', '.docx', '.ppt', '.pptx'}:
        return False
    if PurePosixPath(relative).name.lower() in {'auth.json', 'credentials.json', '.env', 'id_rsa', 'id_ed25519'}:
        raise ValueError('Private configuration must stay in the source: ' + relative)
    return True


def archive_write(archive, name, data):
    if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name:
        raise ValueError('Unsafe archive path: ' + name)
    info = zipfile.ZipInfo(name, (2026, 11, 13, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data, compresslevel=9)


def verify_originals(source):
    checked = 0
    for area in ['runs/collections', 'runs/smoke']:
        for run in sorted(fs(source / area).iterdir()):
            checksums = run / 'checksums.json'
            if checksums.exists():
                for relative, expected in read_json(checksums).items():
                    if sha(fs(run / relative).read_bytes()) != expected:
                        raise ValueError('Original sealed hash mismatch: ' + run.name + '/' + relative)
                    checked += 1
    frozen = 0
    lock = read_json(source / 'config/sources.lock.json')['sources']
    for version in lock.values():
        for name, expected in version.get('files_sha256', {}).items():
            if sha(fs(source / version['path'] / name).read_bytes()) != expected:
                raise ValueError('Pinned source changed: ' + name)
    resource_root = source / lock['benchmark_isolation_candidate_lf']['path']
    for task in read_json(source / 'reports/task-resources/resources.json')['tasks'].values():
        for entry in task.get('data_files', []):
            if entry.get('sha256'):
                if sha(fs(resource_root / entry['path']).read_bytes()) != entry['sha256']:
                    raise ValueError('Frozen task resource mismatch: ' + entry['path'])
                frozen += 1
    return {'sealed_file_hashes_checked': checked, 'frozen_resources_checked': frozen,
            'pinned_source_hashes_checked': sum(len(v.get('files_sha256', {})) for v in lock.values())}


def export(source):
    source = fs(source)
    if source.name != 'rsi-workspace':
        source /= 'rsi-workspace'
    evidence_checks = verify_originals(source)
    print(json.dumps({'original_integrity_verified': evidence_checks}), flush=True)
    complete = read_json(source / 'reports/collection-completion.json')
    if not complete['all_collection_complete'] or complete['issues'] or complete['active_runs']:
        raise ValueError('Source completion receipt is not complete and clean')
    pair = complete['codebase_pair_run']
    manifest = []
    excluded = Counter()

    def record(relative, original, exported, changes, archive=None, member=None, origin=None):
        entry = {'source': origin or relative, 'source_sha256': sha(original),
                 'exported_sha256': sha(exported), 'source_bytes': len(original),
                 'exported_bytes': len(exported), 'normalizations': changes}
        if archive:
            entry.update(archive=archive, member=member or relative)
        else:
            entry['file'] = relative
        manifest.append(entry)

    def loose(relative, original, origin=None):
        clean, changes = sanitize(original, relative)
        fs(PACK / relative).parent.mkdir(parents=True, exist_ok=True)
        fs(PACK / relative).write_bytes(clean)
        record(relative, original, clean, changes, origin=origin)

    rows, episodes = [], []
    with fs(source / 'reports/all-episodes.csv').open(encoding='utf-8-sig', newline='') as stream:
        original_rows = list(csv.DictReader(stream))
    for original_row in original_rows:
        row = dict(original_row)
        original = fs(source / row['episode_path']).read_bytes()
        loose(row['episode_path'], original)
        episode = json.loads(fs(PACK / row['episode_path']).read_text(encoding='utf-8-sig'))
        if episode['native_outcome']['instance_id'] != row['instance_id']:
            raise ValueError('Episode/outcome identity mismatch')
        if episode['native_outcome']['success'] != (row['native_success'] == 'True'):
            raise ValueError('CSV/native success mismatch')
        row['role'] = 'auxiliary_interrupted' if row['task'] == 'codebase_adaptation' and row['run_id'] != pair else 'primary'
        row['native_success'] = episode['native_outcome']['success']
        row['native_reward'] = episode['native_outcome']['reward']
        row['exported_episode_sha256'] = sha(fs(PACK / row['episode_path']).read_bytes())
        rows.append(row)
        episodes.append(episode)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    fs(PACK / 'reports').mkdir(parents=True, exist_ok=True)
    fs(PACK / 'reports/all-episodes.csv').write_bytes(stream.getvalue().encode('utf-8'))
    record('reports/all-episodes.csv', fs(source/'reports/all-episodes.csv').read_bytes(),
           stream.getvalue().encode('utf-8'), {'index_role_and_exported_digest': len(rows)})

    for area in ['runs/collections', 'runs/smoke']:
        for run in sorted(fs(source / area).iterdir()):
            archive_relative = 'raw/' + run.name + '.zip'
            fs(PACK / 'raw').mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(fs(PACK / archive_relative), 'w') as archive:
                for path in sorted(run.rglob('*')):
                    if not path.is_file():
                        continue
                    relative = path.relative_to(source).as_posix()
                    if not approved_run_file(relative):
                        excluded['runtime_cache'] += 1
                        continue
                    original = path.read_bytes()
                    if path.relative_to(run).as_posix() == 'audit/collection-index.json':
                        loose(relative, original)
                    clean, changes = sanitize(original, relative)
                    archive_write(archive, relative, clean)
                    record(relative, original, clean, changes, archive=archive_relative)
            print(json.dumps({'archived_run': run.name, 'compressed_bytes': fs(PACK/archive_relative).stat().st_size}), flush=True)
    # Historical archive: keep execution evidence, omit setup/preflight materials.
    historical_hashes = 0
    historical_archive = 'raw/historical-execution-evidence.zip'
    with zipfile.ZipFile(fs(PACK / historical_archive), 'w') as destination:
        for path in sorted(fs(source / 'archives').glob('process-history-*.zip')):
            with zipfile.ZipFile(path) as original_zip:
                for entry in json.loads(original_zip.read('archive-index.json'))['files']:
                    if sha(original_zip.read(entry['path'])) != entry['sha256']:
                        raise ValueError('Historical archive hash mismatch: ' + entry['path'])
                    historical_hashes += 1
                for info in original_zip.infolist():
                    name = info.filename
                    prefix = 'rsi-workspace/'
                    relative = name[len(prefix):] if name.startswith(prefix) else name
                    if info.is_dir() or not relative.startswith(('runs/', 'reports/collection-suite/history/', 'reports/cli-validation/')):
                        excluded['historical_setup_or_preflight'] += not info.is_dir()
                        continue
                    if not approved_run_file(relative):
                        excluded['runtime_cache'] += 1
                        continue
                    original = original_zip.read(info)
                    clean, changes = sanitize(original, relative)
                    archive_write(destination, relative, clean)
                    record(relative, original, clean, changes, archive=historical_archive,
                           origin='archives/' + path.name + '!' + name)
    evidence_checks['historical_archive_members_checked'] = historical_hashes

    for path in sorted(fs(source / 'reports').rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        report = path.relative_to(source / 'reports').as_posix()
        if report in REPORTS or report.startswith(('collection-suite/', 'protocol-review/', 'task-resources/')):
            loose(relative, path.read_bytes())
    for path in sorted(fs(source / 'config').glob('*')):
        if path.name.startswith('collection-') or path.name in {'images.lock.json', 'sources.lock.json'}:
            loose('config/' + path.name, path.read_bytes())
    for name in sorted(SCRIPTS):
        loose('process/' + name, fs(source / 'scripts' / name).read_bytes(), origin='scripts/' + name)
    loose('licenses/CL-Bench-LICENSE', fs(source / 'sources/continual-learning-bench-pr26-lf/LICENSE').read_bytes(),
          origin='sources/continual-learning-bench-pr26-lf/LICENSE')

    primary = [r for r in rows if r['role'] == 'primary']
    groups = []
    for task in COUNTS:
        phases = ['baseline', 'run'] if task == 'codebase_adaptation' else ['run']
        for phase in phases:
            selected = [r for r in primary if r['task'] == task and r['phase'] == phase]
            passed = sum(r['native_success'] is True for r in selected)
            valid = sum(isinstance(r['native_success'], bool) for r in selected)
            groups.append({'task': task, 'phase': phase, 'records': len(selected),
                           'native_success_true': passed, 'native_success_false': valid-passed,
                           'native_success_null': len(selected)-valid,
                           'native_success_rate': passed / valid if valid else None,
                           'mean_native_reward': sum(float(r['native_reward']) for r in selected)/len(selected),
                           'environment_auto_resolutions': sum(r['evidence_kind'] == 'environment_auto_resolution' for r in selected)})
    stats = read_json(source / 'reports/collection-statistics.json')
    summary = {'benchmark': 'CL-Bench', 'source_sha': PIN,
               'source_publication_status': 'unmerged PR26 including PR25; not the official master baseline',
               'harness': 'native Codex CLI', 'codex_cli_version': '0.167.0',
               'requested_model': 'gpt-6.1-sol', 'api_base_url': 'https://cn.affordablerouter.com/v1',
               'use_oauth': False, 'model_identity_independently_verified': False,
               'collected_in_shared_rsi_platform': False,
               'unique_instances': len({(r['task'],r['instance_id']) for r in primary}),
               'primary_records': len(primary), 'auxiliary_records': len(rows)-len(primary),
               'indexed_records': len(rows), 'primary_native_success_true': sum(r['native_success'] is True for r in primary),
               'primary_native_success_rate': sum(r['native_success'] is True for r in primary)/len(primary),
               'groups': groups, 'evidence_kinds_all_indexed': dict(Counter(r['evidence_kind'] for r in rows)),
               'new_interactions_all_indexed': stats['new_model_interactions_in_indexed_episodes'],
               'replayed_interactions_all_indexed': stats['replayed_interactions_inside_new_partial_episodes'],
               'accuracy_note': 'Native success proportion; reward semantics differ by task. Primary aggregate includes both Codebase phases and is not an official benchmark aggregate.',
               'interaction_note': 'Action/feedback turns, not HTTP request counts. Replay prefixes are not new model work.',
               'cost_note': 'Native cost fields are estimates, not actual gateway billing.',
               'model_calls_during_export': 0}
    assert len(primary) == 320 and len(rows) == 322 and summary['unique_instances'] == 301
    dump(PACK / 'results.json', summary)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(groups[0]), lineterminator='\n')
    writer.writeheader(); writer.writerows(groups)
    fs(PACK/'reports/accuracy-by-task.csv').write_text(stream.getvalue(), encoding='utf-8')
    dump(PACK / 'SOURCE_MANIFEST.json', {'schema_version': 1, 'source_collection': '2026.11.11/rsi-workspace',
         'source_benchmark_sha': PIN, 'source_integrity': evidence_checks,
         'normalization_policy': 'Normalize host account/workspace paths only; inspect and normalize gzip payloads. Preserve original checksum receipts as historical evidence; validate public bytes using exported_sha256 here.',
         'selection_policy': 'All collection/smoke run evidence except caches/private auth; scored episodes also readable; selected final/incident/protocol reports and actual execution code/config; no personal documents, third-party checkout, datasets, or unused plans.',
         'excluded_counts': dict(excluded), 'files': manifest})
    print(json.dumps({'exported_entries': len(manifest), 'primary_records': len(primary),
                      'indexed_records': len(rows), 'source_integrity': evidence_checks}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='2026.11.11 or its rsi-workspace directory')
    export(parser.parse_args().source)
