"""torchrun4 worker for the real H20 actor, fresh validation and CPU shard export."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', type=Path, required=True)
    parser.add_argument('--stage', choices=('check', 'actor', 'validate', 'export'), required=True)
    args = parser.parse_args(argv)
    from training.skillos_h20_runtime import Runtime
    runtime = Runtime(args.context)
    if args.stage == 'check':
        _, _, _, summary, _ = runtime.load_contract()
        runtime.build()
        print(json.dumps({'contract_valid': True, 'summary': summary,
                          'source_adaptations': runtime.sources.audit, 'gpu_executed': False}))
        return 0
    runtime.run(args.stage)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
