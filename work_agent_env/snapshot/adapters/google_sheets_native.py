"""Start the unchanged upstream Sheets MCP service with clean protocol stdout."""
from __future__ import annotations

import builtins
import os
from pathlib import Path
import sys

upstream = Path(os.environ["TOOLATHLON_ROOT"])
sys.path.insert(0, str(upstream / "local_servers" / "mcp-google-sheets" / "src"))
original_print = builtins.print


def diagnostic_print(*args, **kwargs):
    kwargs.setdefault("file", sys.stderr)
    return original_print(*args, **kwargs)


builtins.print = diagnostic_print
from mcp_google_sheets import main  # noqa: E402

main()
