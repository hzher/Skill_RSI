"""Command registration and common rendering."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import subprocess
import sys


def command(routes, name, description, handler=None, *, aliases=()):
    parser = routes.add_parser(name, help=description, description=description, aliases=aliases)
    if handler is not None:
        parser.set_defaults(handler=handler)
    return parser


def common_host(parser, *, required=True):
    parser.add_argument("--host", choices=("dsh", "claude_code", "codex"), required=required)
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--command", help="native CLI executable")


def config_arg(parser, flag="--config", *, required=False):
    parser.add_argument(flag, required=required, type=Path)


def state_root(path):
    from ..components._local import state_root as local_state_root
    return local_state_root(path)


def _version():
    try:
        return version("skill-rsi")
    except PackageNotFoundError:
        return "0.1.0"


def _serial(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return dict(value)
    if callable(getattr(value, "to_dict", None)):
        return value.to_dict()
    raise TypeError(f"unsupported output type: {type(value).__name__}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="skill-rsi", description="Manage evidence and host memory extensions.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    parser.add_argument("--state-dir", type=Path, help="local state root (default: SKILL_RSI_HOME or user state directory)")
    routes = parser.add_subparsers(dest="command", required=True)
    for module in ("hosts", "evidence", "laboratory"):
        import_module(f"{__name__}.{module}").register(routes)
    args = parser.parse_args(argv)
    args.state_dir = state_root(args.state_dir)
    try:
        response = args.handler(args)
        print(json.dumps(response, ensure_ascii=False, allow_nan=False, indent=2, default=_serial))
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print(f"skill-rsi: {exc}", file=sys.stderr)
        return 1
    return args.exit_status(response) if hasattr(args, "exit_status") else 0
