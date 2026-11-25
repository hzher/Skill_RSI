"""Detached process that owns one native task and records its final outcome."""

import argparse
from pathlib import Path
import signal

from ..evidence.io import read_json
from .store import run_task


def _cancel(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _cancel)
    task = read_json(args.task / "task.json")
    request = read_json(args.task / "request.json")
    try:
        result = run_task(task["host"], task["project"], args.state_dir, prompt=task["prompt"],
                          resume=request.get("resume"), options=request.get("options"), task=task)
    except KeyboardInterrupt:
        return 130
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
