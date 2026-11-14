"""Verify provenance, compile source and check pure contracts without services."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

PACK = Path(__file__).resolve().parent
REPO = PACK.parent.parent
SNAPSHOT = PACK / 'snapshot'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    manifest = json.loads((PACK / 'SOURCE_PROVENANCE.json').read_text(encoding='utf-8'))
    errors, key_findings = [], []
    compiled = 0
    fixtures = 0
    for row in manifest['files']:
        path = REPO / row['destination']
        if not path.resolve().is_relative_to(SNAPSHOT.resolve()):
            raise ValueError('Manifest destination escapes the snapshot')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            errors.append({'path': row['source_file'], 'kind': 'source_hash_mismatch'})
        if path.suffix == '.py':
            try:
                compile(data, str(path), 'exec')
                compiled += 1
            except SyntaxError:
                errors.append({'path': row['source_file'], 'kind': 'syntax_error_on_' + sys.version.split()[0]})
        for match in re.finditer(rb'(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{24,}|(?:ghp|gho|github_pat|hf)_[A-Za-z0-9_]{30,})', data):
            candidate = match.group().lower()
            if any(tag in candidate for tag in (b'test', b'example', b'placeholder', b'dummy', b'fake')):
                fixtures += 1
            else:
                key_findings.append({'path': row['source_file'], 'kind': 'credential_shaped_literal'})
        if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{40,}', data):
            key_findings.append({'path': row['source_file'], 'kind': 'private_key_block'})
        name = row['source_file']
        if ('/vendor/' in name or name.startswith(('evidence/', 'tasks/', 'training/candidates/',
                'training/candidate_history/')) or name.endswith(('.sqlite', '.db', '.pt', '.safetensors'))):
            errors.append({'path': name, 'kind': 'excluded_material_in_export'})
    sys.path.insert(0, str(SNAPSHOT))
    from integrations.benchmark_tasks.contracts import make_task, make_task_set, validate_task_set, strict_json
    task = make_task(benchmark='file_report', task_id='greeting',
        source={'revision': '1', 'plugin': 'handoff'}, prompt='Create result.txt',
        environment={'backend': 'docker', 'config': {}}, evaluation={'backend': 'command_json', 'config': {}})
    assert validate_task_set(make_task_set([task]))['tasks'][0]['task_id'] == 'greeting'
    for action in (lambda: make_task_set([task, task]), lambda: strict_json('{"id":1,"id":2}')):
        try:
            action()
        except ValueError:
            pass
        else:
            errors.append({'kind': 'invalid_task_contract_accepted'})
    result = {'status': 'passed' if not errors and not key_findings else 'failed',
        'verified_at': datetime.now(timezone.utc).isoformat(), 'source_sha': manifest['source_sha'],
        'python': sys.version.split()[0], 'exported_files_verified': len(manifest['files']),
        'source_bytes_verified': sum(row['bytes'] for row in manifest['files']),
        'python_files_compiled': compiled, 'pure_contract_checks': 3,
        'source_last_change_authors': dict(Counter(row['author'] for row in manifest['files'])),
        'credential_pattern_findings': key_findings, 'synthetic_key_literals': fixtures,
        'errors': errors, 'model_calls': 0, 'docker_started': False, 'gpu_started': False,
        'scope': 'Git snapshot integrity, syntax and pure contract checks only; no full benchmark/training claim'}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
