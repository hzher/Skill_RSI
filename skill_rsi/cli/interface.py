"""Installed-package UI entry."""

from pathlib import Path
from . import command


def register(routes):
    from ..ui.server import serve

    parser = command(routes, "ui", "Open the local harness and extension manager", serve)
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--tasks-dir", type=Path, help="shared task trajectory folder (default: ~/SkillRSI/tasks)")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
