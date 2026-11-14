"""Allow passwordless task email identities in the PostgreSQL-only MCP server."""

from pathlib import Path


path = Path("/opt/local_servers/emails-mcp/src/emails_mcp/config/settings.py")
source = path.read_text()
old = "password=account_data.get('password', ''),"
new = "password=account_data.get('password') or 'unused',"
if source.count(new) == 1:
    pass
elif source.count(old) != 1:
    raise RuntimeError("Expected email configuration assignment not found exactly once")
else:
    path.write_text(source.replace(old, new))
