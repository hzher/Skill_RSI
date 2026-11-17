"""CLI for the read-only native evidence pipeline."""

import argparse
import json

from .build import HARNESSES, build
from .verify import verify


def main():
    parser = argparse.ArgumentParser(prog="python -m evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    build_cmd = commands.add_parser("build", help="generate evidence from an existing batch")
    build_cmd.add_argument("batch_dir")
    build_cmd.add_argument("--harness", choices=HARNESSES, default="dsh")
    build_cmd.add_argument("--source", action="append", help="Claude Code or Codex official trace path relative to the batch")
    build_cmd.add_argument("--output", help="override the derived evidence directory")
    build_cmd.add_argument("--seal", action="store_true", help="freeze referenced source files inside each protocol run")
    verify_cmd = commands.add_parser("verify", help="verify an existing derived archive")
    verify_cmd.add_argument("archive")
    verify_cmd.add_argument("--batch-dir", help="source directory for externally stored unsealed evidence")
    args = parser.parse_args()
    if args.command == "build":
        result = build(args.batch_dir, args.harness, output=args.output, sources=args.source, seal=args.seal)
    else:
        result = verify(args.archive, batch_dir=args.batch_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
