"""Run the Codex-owned native plugin MCP service from a frozen launch file."""
import json
from pathlib import Path
import sys

from ...evidence.archive import write
from ._native.codex_mcp import base_catalog
from ._native.bounded_mcp import serve
from ._native.plugins import PluginHarness
from .components import native_digest


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise ValueError('Expected one explicitly selected MCP launch configuration')
    path = Path(args[0]).resolve(strict=True)
    config = json.loads(path.read_text())
    if config.get('native_code_digest') != native_digest():
        raise ValueError('The frozen MCP launch belongs to another native implementation')
    harness = PluginHarness(config)
    extra, _ = harness.tools()
    write(path.parent / 'mcp-loaded.json', {
        'status': 'loaded', 'native_code_digest': native_digest(),
        'components': config.get('component_bindings', []),
        'tools': [item['name'] for item in base_catalog(config, extra)],
        'model_input_visibility': 'unknown',
    })
    serve(config)


if __name__ == '__main__':
    main()
