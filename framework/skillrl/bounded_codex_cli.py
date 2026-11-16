#!/usr/bin/env python3
"""Codex CLI entry point selecting the versioned bounded-history MCP server."""
import hashlib
import json
import os
from pathlib import Path
import sys

args = [arg.replace('"skillrl.codex_mcp"', '"skillrl.bounded_mcp"')
        if arg.startswith('mcp_servers.benchmark=') else arg for arg in sys.argv[1:]]
if '-o' in args:
    actor = Path(args[args.index('-o') + 1]).parent
    (actor / 'bounded-mcp-command.json').write_text(json.dumps({
        'argv': ['codex', *args], 'wrapper_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'history_view': 'nonrecursive-history-v1; original authorized source retained'}, indent=2) + '\n')
os.execvp('codex', ['codex', *args])
