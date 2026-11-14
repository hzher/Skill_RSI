"""Export the approved first-party handoff from a pinned Git tree, never a cwd.

No model, Docker, SSH, dataset download or dependency installation is performed.
Existing different files are refused, so this command cannot erase local edits.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess

PIN = 'aa07d24e308e3adfa843ca70e3faf1ccfa8185eb'
SOURCE_PROJECT = 'work_agent_env'
TARGET = Path(__file__).resolve().parents[1] / 'baselines/work_agent_env'
CODE_ROOTS = {'adapters', 'workenv', 'integrations', 'resources_servers',
              'responses_api_agents', 'training', 'evaluation', 'scripts', 'tests'}
CONFIGS = {'configs/.env.example', 'configs/task_adapter_profiles.json',
           'configs/toolathlon.dockerignore',
           'configs/upstreams.lock.json', 'configs/mainline_sources.lock.json',
           'configs/markdown_training.example.json', 'configs/model_endpoints.example.json'}
GUIDES = {'docs/architecture.md', 'docs/guides/generic_benchmarks.md',
          'docs/guides/benchmark_tasks.md', 'docs/guides/environments.md',
          'docs/guides/executors.md', 'docs/guides/experience_skills.md',
          'docs/guides/async_skill_training.md', 'docs/guides/skillos_upstream.md',
          'docs/guides/skillos_roles.md', 'docs/guides/skillos_h20.md',
          'docs/guides/task_packages_verifiers.md', 'docs/guides/training_and_evaluation.md'}


def decision(name):
    path = PurePosixPath(name)
    parts = path.parts
    if any(part in {'vendor', '.git', '.runtime', '.venv', 'node_modules', '__pycache__'} for part in parts):
        return False, 'vendored or machine-local material'
    if name.startswith('integrations/benchmarks/file_report/'):
        return True, 'first-party public diagnostic fixture'
    if name.startswith(('integrations/benchmarks/', 'tasks/', 'evidence/')):
        return False, 'benchmark data, task payloads, private traces or historical artifacts'
    if name in CONFIGS or name in GUIDES:
        return True, 'public interface/reference configuration or technical guide'
    if parts[0] == 'configs' and ('requirements' in path.name or '.package' in path.name):
        return True, 'dependency declaration or lock'
    if parts[0] in CODE_ROOTS and path.suffix in {'.py', '.mjs', '.sh'}:
        return True, 'first-party implementation or regression test'
    if parts[0] in {'resources_servers', 'responses_api_agents'} and path.name == 'requirements.txt':
        return True, 'service dependency declaration'
    if parts[0] == 'docker' and (path.name.startswith('Dockerfile') or path.name.endswith('.Dockerfile') or path.suffix in {'.py', '.ts', '.sh'}):
        return True, 'first-party runtime build/adapter source; external assets not bundled'
    return False, 'deployment configuration, generated candidate/catalog or nonessential historical material'


def git(source, *args):
    return subprocess.check_output(['git', '-C', str(source), *args])


def export(source):
    source = Path(source).resolve()
    head = git(source, 'rev-parse', 'HEAD').decode().strip()
    if head != PIN:
        raise ValueError('Source HEAD must be the pinned handoff commit ' + PIN)
    if git(source, 'status', '--porcelain=v1', '--untracked-files=normal').strip():
        raise ValueError('Source has local changes; use the reviewed clean source')
    existing = TARGET / 'SOURCE_PROVENANCE.json'
    if existing.exists():
        previous = json.loads(existing.read_text(encoding='utf-8'))
        if previous['source_sha'] != PIN:
            raise ValueError('Refuse to replace an export from another source')
        for row in previous['files']:
            path = TARGET / 'snapshot' / row['source_file']
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
                raise ValueError('Existing snapshot was edited; do not overwrite it: ' + row['source_file'])
    entries, excluded = [], Counter()
    for raw in git(source, 'ls-tree', '-rz', PIN).split(b'\0'):
        if not raw:
            continue
        meta, raw_name = raw.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        name = raw_name.decode('utf-8')
        approved, reason = decision(name)
        if approved:
            if kind != 'blob' or mode not in ('100644', '100755'):
                raise ValueError('Only ordinary source files may be exported: ' + name)
            entries.append({'source_file': name, 'git_blob': oid, 'git_mode': mode, 'reason': reason})
        else:
            excluded[reason] += 1
    # Keep revision evidence without exporting personal attribution metadata.
    log = git(source, 'log', '--no-renames', '--format=%x1e%H%x1f%cI', '--name-only', PIN)
    wanted = {row['source_file'] for row in entries}
    revisions = {}
    for block in log.decode('utf-8').split('\x1e'):
        lines = block.strip().splitlines()
        if not lines or '\x1f' not in lines[0]:
            continue
        commit, date = lines[0].split('\x1f', 1)
        for name in lines[1:]:
            if name in wanted and name not in revisions:
                revisions[name] = {'last_change_commit': commit, 'commit_date': date}
    snapshot = TARGET / 'snapshot'
    snapshot.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(['git', '-C', str(source), 'cat-file', '--batch'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for row in entries:
            process.stdin.write((row['git_blob'] + '\n').encode('ascii'));process.stdin.flush()
            oid, kind, size = process.stdout.readline().decode('ascii').strip().split()
            if oid != row['git_blob'] or kind != 'blob':
                raise ValueError('Unexpected Git object response')
            data = process.stdout.read(int(size))
            if len(data) != int(size) or process.stdout.read(1) != b'\n':
                raise ValueError('Truncated Git object')
            destination = snapshot / row['source_file']
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.read_bytes() != data:
                    raise ValueError('Refuse to overwrite a different local file: ' + str(destination))
            else:
                destination.write_bytes(data)
            row.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                       destination=destination.relative_to(TARGET.parent.parent).as_posix(),
                       **revisions.get(row['source_file'], {'last_change_commit': None}))
    finally:
        process.stdin.close()
        process.wait(timeout=15)
    provenance = {'source_repository': SOURCE_PROJECT, 'source_branch': 'main', 'source_sha': PIN,
        'target_repository': 'https://github.com/hzher/Skill_RSI', 'target_branch': 'sunchengze',
        'shared_base': 'd068b79deb0e71f2f55e88487efff051dadd0f0a',
        'exported_at': datetime.now(timezone.utc).isoformat(),
        'byte_contract': 'Exact Git blob bytes. Windows working-tree CRLF conversions are not the source version.',
        'license': 'No standalone LICENSE found in source tree. No automatic relicensing under target root MIT.',
        'files': entries, 'excluded_counts': dict(excluded), 'native_bench_or_training_claimed': False}
    (TARGET / 'SOURCE_PROVENANCE.json').write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'exported_files': len(entries), 'source': PIN,
                      'snapshot_bytes': sum(row['bytes'] for row in entries),
                      'model_calls': 0, 'excluded_files': sum(excluded.values())}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    export(parser.parse_args().source)
