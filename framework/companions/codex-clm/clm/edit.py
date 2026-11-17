#!/usr/local/bin/python3
"""Codex tool-side interface for inspecting and editing live context."""

import argparse
import json
import os
from pathlib import Path

from context import write_json


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("show").add_argument("id")
    edit = commands.add_parser("edit")
    edit.add_argument("--drop", nargs="+", required=True)
    edit.add_argument("--note", help="Replace assistant memory. Omit to preserve it.")
    args = parser.parse_args()
    root = Path(os.environ["CLM_CONTEXT_DIR"])
    snapshot = json.loads((root / "LIVE_CTX_MAIN.json").read_text())
    if args.command == "status":
        snapshot["editable"] = [
            {key: item[key] for key in ("id", "bytes", "tool_names")} for item in snapshot["editable"]
        ]
        print(json.dumps(snapshot, ensure_ascii=False))
    elif args.command == "show":
        for item in snapshot["editable"]:
            if item["id"] == args.id:
                text = json.dumps(item["items"], ensure_ascii=False)
                print(text)
                return
        parser.error("no editable exchange with that ID")
    else:
        if not set(args.drop) <= {item["id"] for item in snapshot["editable"]}:
            parser.error("one or more IDs are not editable in this revision")
        write_json(root / "edit.json", {
            "revision": snapshot["revision"],
            "drop": args.drop,
            "note": snapshot["note"] if args.note is None else args.note,
        })
        print("Submitted. Use clm-context status on the next turn to verify acceptance.")


if __name__ == "__main__":
    main()
