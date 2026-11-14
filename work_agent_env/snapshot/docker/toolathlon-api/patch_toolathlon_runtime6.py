"""Keep JSON escaping in WooCommerce MCP responses so consumers can parse them."""
from pathlib import Path

path = Path('/opt/local_servers/woocommerce-mcp/src/server.ts')
source = path.read_text()
old = r"JSON.stringify(result, null, 2).replace(/\\/g, '')"
assert source.count(old) == 1, 'Expected one WooCommerce response serializer'
path.write_text(source.replace(old, 'JSON.stringify(result, null, 2)'))
