"""Recompute two fixed email identities from public sources; never run a tool."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.email_task_identity import describe, audit_review, REPORT
from workenv.core import save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Recompute the existing receipt without overwriting it")
    args = parser.parse_args()
    if args.check: result = audit_review(ROOT, ROOT / REPORT)
    else:
        result = describe(ROOT)
        save(ROOT / REPORT, result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__": raise SystemExit(main())
