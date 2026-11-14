"""Use Canvas's PostgreSQL router in isolated Gym task containers."""

from pathlib import Path


path = Path("/opt/local_servers/mcp-canvas-lms/src/client.ts")
source = path.read_text()
changes = [
    ("import axios, { AxiosInstance, AxiosError } from 'axios';",
     "import axios, { AxiosInstance, AxiosError } from 'axios';\nimport { PgCanvasRouter } from './pg-canvas-router.js';"),
    ("""    this.client = axios.create({
      baseURL: this.baseURL,
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      timeout: 30000 // 30 second timeout
    });

    this.setupInterceptors();""",
     """    if (process.env.PG_HOST) {
      this.client = new PgCanvasRouter() as unknown as AxiosInstance;
    } else {
      this.client = axios.create({
        baseURL: this.baseURL,
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        timeout: 30000 // 30 second timeout
      });
      this.setupInterceptors();
    }"""),
]
for old, new in changes:
    if source.count(new) == 1:
        continue
    if source.count(old) != 1:
        raise RuntimeError(f"Expected Canvas source not found exactly once: {old[:60]!r}")
    source = source.replace(old, new)
path.write_text(source)
