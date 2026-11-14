"""Check the exported public sample and existing generic tests with no secrets."""
import argparse
from datetime import datetime, timezone
import importlib.metadata
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / 'baselines/work_agent_env'
SNAPSHOT = PACK / 'snapshot'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=PACK / 'CHECKS.json')
    args = parser.parse_args()
    scratch = tempfile.TemporaryDirectory(prefix='work-agent-handoff-check-')
    temporary_root = Path(scratch.name).resolve()
    if temporary_root.parent != Path(tempfile.gettempdir()).resolve():
        raise ValueError('Unexpected temporary workspace root')
    working = temporary_root / 'snapshot'
    shutil.copytree(SNAPSHOT, working, ignore=shutil.ignore_patterns('.runtime', '.venv', '__pycache__', 'node_modules'))
    manifest = json.loads((PACK / 'SOURCE_PROVENANCE.json').read_text(encoding='utf-8'))
    for row in manifest['files']:
        if hashlib.sha256((working / row['source_file']).read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('Temporary test copy differs from the pinned export')
    env = dict(os.environ)
    for name in ('MODEL_API_KEY', 'MODEL_API_URL', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY',
                 'OPENAI_BASE_URL', 'WORKENV_OPENAI_BASE_URL', 'WORKENV_CHAT_API_KEY', 'WORKENV_CLAUDE_BASE_URL'):
        env.pop(name, None)
    env.update(RUN_CONTAINER_CHECKS='0', PYTHONPATH=str(working), PYTHONDONTWRITEBYTECODE='1')
    logs = ROOT / '.runtime/handoff-checks' / uuid.uuid4().hex
    logs.mkdir(parents=True, exist_ok=False)
    script = str(working / 'scripts/benchmark_tasks.py')
    commands = [('integrity', [sys.executable, str(PACK / 'verify_handoff.py'), '--output', str(PACK / 'verification.json')]),
                ('list', [sys.executable, script, 'list']),
                ('static-package', [sys.executable, script, 'test', '--benchmark', 'file_report']),
                ('plan', [sys.executable, script, 'run', '--benchmark', 'file_report', '--provider', 'openai',
                          '--model', 'handoff-no-inference', '--run-id', 'handoff-plan-' + uuid.uuid4().hex[:12]]),
                ('generic-tests', [sys.executable, '-m', 'unittest', 'discover', '-s', str(working / 'tests'),
                                   '-p', 'test_generic_runtime.py', '-v'])]
    rows = []
    for label, command in commands:
        result = subprocess.run(command, cwd=working, env=env, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=120)
        (logs / (label + '.stdout.log')).write_text(result.stdout, encoding='utf-8')
        (logs / (label + '.stderr.log')).write_text(result.stderr, encoding='utf-8')
        row = {'step': label, 'exit_code': result.returncode}
        if label == 'generic-tests':
            match = re.search(r'Ran (\d+) tests?', result.stderr)
            skipped = re.search(r'OK \(skipped=(\d+)\)', result.stderr)
            row.update(tests_run=int(match.group(1)) if match else None,
                       skipped=int(skipped.group(1)) if skipped else 0)
        elif result.returncode == 0:
            value = json.loads(result.stdout)
            if label == 'list':
                row['benchmarks'] = value['benchmarks']
                if row['benchmarks'] != ['file_report']:
                    raise ValueError('Unexpected bundled benchmark data')
            if label == 'static-package':
                row.update(static_valid=value['static_valid'], tasks=value['tasks'], model_calls=value['model_calls'])
            if label == 'plan':
                row.update(status=value['status'], tasks=value['tasks'])
                if value['status'] != 'planned_not_executed':
                    raise ValueError('Dry plan unexpectedly executed')
        rows.append(row)
    versions = {}
    for name in ('jsonschema', 'attrs', 'jsonschema-specifications', 'referencing', 'rpds-py', 'typing-extensions'):
        try: versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: versions[name] = None
    passed = all(row['exit_code'] == 0 for row in rows)
    report = {'status': 'passed' if passed else 'failed', 'checked_at': datetime.now(timezone.utc).isoformat(),
        'source_sha': 'aa07d24e308e3adfa843ca70e3faf1ccfa8185eb', 'python': sys.version.split()[0],
        'steps': rows, 'test_dependencies': versions, 'logs': str(logs.relative_to(ROOT)),
        'credentials_removed_from_child_environment': True, 'model_calls': 0, 'docker_started': False,
        'execution_workspace': 'isolated temporary snapshot; exported files rehashed after copying',
        'gpu_started': False, 'shared_framework_modified': False,
        'limitations': ['Container cases intentionally skipped; no native Docker runtime acceptance in this handoff.',
                        'Only public file_report diagnostic is bundled; private task/data/vendor tests need external resources.',
                        'No Skill benefit, full benchmark or training/weight-update result is claimed.']}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    scratch.cleanup()
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
