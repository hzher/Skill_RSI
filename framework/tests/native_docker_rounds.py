"""Opt-in native Claude Code + Docker test with simulated provider, never a benchmark score."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from support import Provider, bundle, proposal, skill, seed
from skillrl.native import ClaudeCodeExecutor
from skillrl.benchmarks import ManifestBenchmark
from skillrl.environments import DockerEnvironment
from skillrl.curation import Curator
from skillrl.review import Reviewer
from skillrl.pipeline import Pipeline
from skillrl.storage import load

class NativeProvider(Provider):
    def reply(self, payload):
        if payload['model'] != 'executor':
            return super().reply(payload)
        messages = payload['messages']
        outputs = [message for message in messages if message['role'] == 'tool']
        if not outputs:
            return self.call('Read', {'file_path': '/opt/skillrl-skills/sum-check.md'}, 0)
        if len(outputs) == 1:
            command = 'python3 -c "import json,pathlib; p=pathlib.Path(\"/workspace/output\"); p.mkdir(exist_ok=True); d=json.load(open(\"/workspace/input.json\")); (p/\"result.json\").write_text(json.dumps({\"sum\":sum(d[\"values\"])}))"'
            # A shell-safe, literal Python heredoc avoids quoting through another shell.
            command = "python3 - <<'PYCODE'\nimport json\nfrom pathlib import Path\np=Path('/workspace/output')\np.mkdir(exist_ok=True)\nd=json.load(open('/workspace/input.json'))\n(p/'result.json').write_text(json.dumps({'sum':sum(d['values'])}))\nPYCODE"
            return self.call('Bash', {'command': command, 'description': 'Compute the public fixture aggregate'}, 1)
        return {'role': 'assistant', 'content': 'The result file was written from the public input.'}

class NativeBenchmark(ManifestBenchmark):
    def __init__(self, fixture, image, network):
        super().__init__(fixture.manifest, image=image, evaluator=fixture.evaluator,
                         network=network, user='1005:1005', security_profile='platform')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--network', required=True)
    parser.add_argument('--gateway', required=True)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=10)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    fixture = bundle(args.root, count=args.rounds)
    benchmark = NativeBenchmark(fixture, args.image, args.network)
    transactions = [proposal(children=[skill('result-check')])] + [proposal('noop')] * (args.rounds - 1)
    with NativeProvider(transactions) as server:
        executor = ClaudeCodeExecutor(server.model('executor'), cli_host_path=args.cli,
             bridge_bind=args.gateway, bridge_advertise=args.gateway, max_turns=12, timeout=120)
        run = Pipeline(args.root / 'run', benchmark, executor, Curator(server.model('curator')),
                       Reviewer(server.model('judge')), sequence='test')
        seed(run.root, ['sum-check'])
        report = run.run()
        summaries = []
        for status in report['episodes']:
            task = status['task_id']
            ep = run.root / 'episodes' / task
            execution = load(ep / 'execution.json') if (ep / 'execution.json').exists() else {}
            exposure = load(ep / 'exposure.json') if (ep / 'exposure.json').exists() else []
            native_exit = load(ep / 'actor/native-exit.json') if (ep / 'actor/native-exit.json').exists() else {}
            outcome_path = run.root / 'evaluation' / task / 'objective/result.json'
            summaries.append({'task_id': task, 'status': status, 'native_exit': native_exit,
                              'tool_calls': len(execution.get('native_calls', [])),
                              'tool_returns': len(execution.get('native_returns', [])),
                              'body_proven': sum(row['body_proven'] for row in exposure),
                              'objective': load(outcome_path) if outcome_path.exists() else None})
        result = {'kind': 'real_native_Claude_Code_Docker_and_scripted_provider', 'real_provider_calls': 0,
                  'rounds': summaries, 'passed': report['complete'], 'cost': report['cost'],
                  'image': args.image, 'security_profile': 'platform',
                  'cli_sha256': hashlib.sha256(args.cli.read_bytes()).hexdigest()}
        (args.root / 'NATIVE_RESULT.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result), flush=True)
        raise SystemExit(0 if result['passed'] else 1)

if __name__ == '__main__':
    main()
