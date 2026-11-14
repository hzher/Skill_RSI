"""Enable WooCommerce's PostgreSQL fixture router in the runtime image."""

from pathlib import Path


path = Path("/opt/local_servers/woocommerce-mcp/src/services/base.ts")
source = path.read_text()
changes = [
    ("import axios, { AxiosInstance } from 'axios';",
     "import axios, { AxiosInstance } from 'axios';\nimport { PgRestRouter } from './pg-rest-router.js';"),
    ("        const baseURL = `${this.siteUrl}/wp-json/wc/v3`;",
     """        const baseURL = `${this.siteUrl}/wp-json/wc/v3`;

        if (process.env.PG_HOST) {
            this.client = new PgRestRouter() as unknown as AxiosInstance;
            console.error('[WooCommerce] Using PostgreSQL fixture backend');
            return;
        }"""),
]
for old, new in changes:
    if source.count(new) == 1:
        continue
    if source.count(old) != 1:
        raise RuntimeError(f"Expected WooCommerce source not found exactly once: {old[:60]!r}")
    source = source.replace(old, new)
path.write_text(source)
