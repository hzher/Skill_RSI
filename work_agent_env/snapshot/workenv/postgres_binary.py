"""Locate PostgreSQL tools without changing the ownership of cluster data."""
import os
from pathlib import Path
import shutil


TOOLS = frozenset({"initdb", "pg_ctl", "psql", "createdb", "pg_dump"})


def postgres_binary(root, name):
    if name not in TOOLS:
        raise ValueError("Unsupported PostgreSQL program")
    root = Path(root)
    suffix = ".exe" if os.name == "nt" else ""
    candidates = [root / ".runtime/pgsql/bin" / (name + suffix)]
    if os.name != "nt":
        configured = os.environ.get("WORKENV_PG_BIN")
        if configured:
            candidates.append(Path(configured) / name)
        candidates.append(Path("/usr/lib/postgresql/15/bin") / name)
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for path in candidates:
        if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
            return path
    raise RuntimeError(f"PostgreSQL {name} binary is unavailable")
