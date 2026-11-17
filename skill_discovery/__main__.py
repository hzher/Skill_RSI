import argparse
import json
from pathlib import Path

from .artifacts import read_json
from .extract import extract
from .validate import evaluate_validation, freeze_validation


def main():
    parser = argparse.ArgumentParser(description="Analyze frozen trajectory evidence and compare skill effects")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("extract", help="Call the explicitly configured teacher on frozen evidence")
    run.add_argument("--archive", type=Path, required=True)
    run.add_argument("--batch-dir", type=Path)
    run.add_argument("--id", required=True)
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--teacher", type=Path, required=True)
    freeze = commands.add_parser("freeze-validation", help="Freeze a paired experiment before running either arm")
    evaluate = commands.add_parser("evaluate-validation", help="Compare the independent recorded validation runs")
    for command in (freeze, evaluate):
        command.add_argument("--extraction", type=Path, required=True)
        command.add_argument("--id", required=True)
    freeze.add_argument("--protocol", type=Path, required=True)
    evaluate.add_argument("--results", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "extract":
            value = extract(args.archive, args.id, read_json(args.input), read_json(args.teacher), args.batch_dir)
        elif args.command == "freeze-validation":
            value = freeze_validation(args.extraction, args.id, read_json(args.protocol))
        else:
            value = evaluate_validation(args.extraction, args.id, read_json(args.results))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"{type(exc).__name__}: {exc}\n")
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    return int(value.get("extraction_status") == "error")


if __name__ == "__main__":
    raise SystemExit(main())
