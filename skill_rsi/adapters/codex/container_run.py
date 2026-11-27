"""Run the managed Codex adapter inside an existing benchmark container."""
import argparse
import json
import os
import pwd
from pathlib import Path
import shutil

from . import Adapter
from ...evidence.archive import write
from ...evidence.verify import verify
from ...pipelines.evidence import ingest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    try:
        os.environ['HOME'] = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        os.environ['HOME'] = '/tmp/skill-rsi-home'
    Path(os.environ['HOME']).mkdir(parents=True, exist_ok=True)
    Path(os.environ['CODEX_HOME']).mkdir(parents=True, exist_ok=True, mode=0o700)
    output = Path(config['output'])
    options = {'command': '/opt/skill-rsi-codex/bin/codex', 'model': config['model'],
        'timeout': config.get('timeout', 1800), 'output': str(output), 'observe': True,
        'api': {'upstream': config['relay_url'], 'credential_env': 'SKILL_RSI_RELAY_KEY', 'timeout': 300},
        'native_plugin': False, 'native_plugin_install': False, 'baseline_tools': False,
        'args': ['--sandbox', 'danger-full-access', '--dangerously-bypass-hook-trust',
                 '-c', 'approval_policy="never"', '-c', 'web_search="disabled"',
                 '-c', 'model_reasoning_effort=' + json.dumps(config.get('reasoning_effort', 'high'))]}
    launch = Adapter().launch(project=config['project'], state_dir=config['state_dir'],
        prompt=config['instruction'], resume=config.get('resume'), options=options)
    sessions = Path(os.environ['CODEX_HOME']) / 'sessions'
    if sessions.exists():
        shutil.copytree(sessions, output / 'native-session')
    archive = output.parent / (output.name + '-evidence')
    try:
        ingest(output, output=archive, host='codex', format='cli', seal=True)
        verify(archive)
        collection = {'status': 'verified', 'archive': str(archive)}
    except (OSError, ValueError) as error:
        collection = {'status': 'failed', 'reason': type(error).__name__, 'capture_preserved': str(output)}
    write(output / 'collection.json', collection)
    print(json.dumps({'launch_status': launch['status'], 'session_id': launch.get('session_id'),
                      'collection_status': collection['status']}), flush=True)
    raise SystemExit(0 if launch['status'] == 'completed' else 1)


if __name__ == '__main__':
    main()
