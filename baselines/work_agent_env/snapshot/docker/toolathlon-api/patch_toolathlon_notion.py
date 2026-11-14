"""Route Gym's Notion MCP to its PostgreSQL backend when PG_HOST is set."""

from pathlib import Path


root = Path("/opt/local_servers/notion-mcp-server/src/openapi-mcp-server")
patches = {
    root / "mcp/proxy.ts": [
        ("import { HttpClient, HttpClientError } from '../client/http-client.js'",
         "import { HttpClient, HttpClientError } from '../client/http-client.js'\nimport { PgHttpClient } from '../client/pg-client.js'"),
        ("  private httpClient: HttpClient\n", "  private httpClient: HttpClient | PgHttpClient\n"),
        ("    this.httpClient = new HttpClient(\n",
         "    const Client = process.env.PG_HOST ? PgHttpClient : HttpClient\n    this.httpClient = new Client(\n"),
    ],
    root / "auth/page-access-control.ts": [
        ("import { HttpClient } from '../client/http-client.js'",
         "import { HttpClient } from '../client/http-client.js'\nimport { PgHttpClient } from '../client/pg-client.js'"),
        ("  httpClient: HttpClient\n", "  httpClient: HttpClient | PgHttpClient\n"),
        ("  private httpClient: HttpClient\n", "  private httpClient: HttpClient | PgHttpClient\n"),
    ],
}

for path, changes in patches.items():
    source = path.read_text()
    for old, new in changes:
        if source.count(new) == 1:
            continue
        if source.count(old) != 1:
            raise RuntimeError(f"Expected Notion source not found exactly once: {path}: {old!r}")
        source = source.replace(old, new)
    path.write_text(source)
