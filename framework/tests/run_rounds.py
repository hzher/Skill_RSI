"""Run complete, independently ordered regression rounds and retain machine-readable receipts."""
from pathlib import Path
import argparse
import hashlib
import io
import json
import platform
import random
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

def leaves(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from leaves(item)
        else:
            yield item

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rounds', type=int, default=10)
    parser.add_argument('--out', type=Path, default=ROOT / 'reports' / 'local-ten-rounds.json')
    args = parser.parse_args()
    if args.rounds < 10:
        raise ValueError('This release gate requires at least ten complete rounds')
    rounds = []
    for number in range(1, args.rounds + 1):
        tests = list(leaves(unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py')))
        random.Random(number).shuffle(tests)
        order = [test.id() for test in tests]
        output = io.StringIO()
        start = time.monotonic()
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.TestSuite(tests))
        rounds.append({'round': number, 'passed': result.wasSuccessful(), 'tests': result.testsRun,
                       'seconds': time.monotonic() - start, 'order': order, 'log': output.getvalue()})
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({'kind': 'scripted_model_real_HTTP_contract_and_end_to_end_tests',
            'real_provider_calls': 0, 'python': platform.python_version(), 'platform': platform.platform(),
            'rounds': rounds, 'passed': all(row['passed'] for row in rounds),
            'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for parent in ['skillrl', 'tests'] for p in sorted((ROOT / parent).rglob('*.py'))}}, indent=2)+'\n')
        print(json.dumps({'round': number, 'tests': result.testsRun, 'passed': result.wasSuccessful()}), flush=True)
        if not result.wasSuccessful():
            print(output.getvalue(), flush=True)
            raise SystemExit(1)

if __name__ == '__main__':
    main()
