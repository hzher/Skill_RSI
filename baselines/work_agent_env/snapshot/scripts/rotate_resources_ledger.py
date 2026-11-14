"""Plan or explicitly rotate an idle, stopped resource ledger without forgetting IDs."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.ledger_rotation import rotation_plan, rotate_ledger, recover_rotation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", choices=("original", "synthetic", "isolated"), default="synthetic")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--recover", action="store_true")
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    path = ROOT / ".runtime/nemo-vm" / {"original": "resource-sessions.json", "synthetic": "synthetic-sessions.json", "isolated": "isolated-sessions.json"}[args.ledger]
    if args.execute:
        if not args.expected_sha256: parser.error("--execute requires the SHA256 from the reviewed plan")
        result = rotate_ledger(path, ROOT, expected_sha256=args.expected_sha256)
    elif args.recover: result = recover_rotation(path, ROOT)
    else: result = rotation_plan(path, ROOT)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__": raise SystemExit(main())
