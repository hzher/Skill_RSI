"""Manage only the PostgreSQL cluster owned by this construction workspace."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

from record_command import ROOT, decode, now
sys.path.insert(0, str(ROOT))
from workenv.postgres_binary import postgres_binary

RUNTIME = ROOT / ".runtime"
DATA = RUNTIME / "pgdata"
BIN = RUNTIME / "pgsql" / "bin"
OWNER = RUNTIME / "cluster_owner.json"
PORT = 5432  # Upstream verifier hardcodes this port.


def require_owner() -> None:
    if not OWNER.exists() or json.loads(OWNER.read_text())["workspace"] != str(ROOT):
        raise RuntimeError("Cluster ownership marker missing or does not match workspace")
    if not DATA.resolve().is_relative_to(ROOT.resolve()):
        raise RuntimeError("Cluster is outside workspace")


def command(name: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PGPASSWORD"] = "camel"  # Upstream's public, local mock credential.
    env["PGCLIENTENCODING"] = "UTF8"
    env["LC_MESSAGES"] = "C"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    argv = [str(postgres_binary(ROOT, name)), *args]
    if name == "pg_ctl" and args[-1:] == ("start",):
        # On Windows a background postgres can inherit capture pipes and keep
        # communicate() waiting after pg_ctl has exited. Use an owned log file.
        log = RUNTIME / "pg_ctl_start.log"
        with log.open("wb") as output:
            p = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                               stdout=output, stderr=subprocess.STDOUT,
                               timeout=120, creationflags=flags)
        print(decode(log.read_bytes()), end="")
    else:
        p = subprocess.run(argv, env=env, capture_output=True, timeout=120, creationflags=flags)
        print(decode(p.stdout), end="")
        print(decode(p.stderr), end="", file=sys.stderr)
    if check and p.returncode:
        raise RuntimeError(f"{name} returned {p.returncode}")
    return p


def status() -> bool:
    require_owner()
    return command("pg_ctl", "-D", str(DATA), "status", check=False).returncode == 0


def start() -> None:
    postgres_binary(ROOT, "initdb")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if DATA.exists():
        require_owner()
        if status():
            return
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", PORT)) == 0:
            raise RuntimeError("Port 5432 is in use; will not change an unrelated server")
    if not DATA.exists():
        OWNER.write_text(json.dumps({"workspace": str(ROOT), "created_at": now(), "port": PORT}), encoding="utf-8")
        pwfile = RUNTIME / "initdb.mock-password"
        pwfile.write_text("camel\n", encoding="utf-8")
        try:
            command("initdb", "-D", str(DATA), "-U", "eigent", "--encoding=UTF8", "--locale=C",
                    "--auth-host=md5", "--auth-local=trust", "--pwfile=" + str(pwfile))
        finally:
            pwfile.unlink(missing_ok=True)
    require_owner()
    command("pg_ctl", "-D", str(DATA), "-l", str(RUNTIME / "postgres.log"),
            "-o", f"-h 127.0.0.1 -p {PORT} -c shared_buffers=32MB -c max_connections=20",
            "-w", "start")


def init_database() -> None:
    if not status():
        raise RuntimeError("Start the owned PostgreSQL cluster first")
    dump = ROOT / ".upstream" / "toolathlon-gym" / "db" / "init.sql.gz"
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    marker = RUNTIME / "database_initialized.json"
    if marker.exists():
        if json.loads(marker.read_text())["dump_sha256"] != digest:
            raise RuntimeError("Dump changed: use a new dedicated cluster, not automatic overwrite")
        print("Database already initialized from the same dump")
        return
    conn_args = ("-h", "127.0.0.1", "-p", str(PORT), "-U", "eigent", "-v", "ON_ERROR_STOP=1")
    command("psql", *conn_args, "-d", "postgres", "-c", "CREATE DATABASE toolathlon_gym")
    sql = RUNTIME / "init.sql"
    with gzip.open(dump, "rb") as source, sql.open("wb") as dest:
        shutil.copyfileobj(source, dest)
    command("psql", *conn_args, "-d", "toolathlon_gym", "-f", str(sql))
    record = {"initialized_at": now(), "dump_sha256": digest,
              "database": "toolathlon_gym", "host": "127.0.0.1", "port": PORT,
              "scope": "dedicated project-local cluster"}
    marker.write_text(json.dumps(record, indent=2), encoding="utf-8")
    (ROOT / "evidence" / "database_initialization.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "init-db", "status", "stop"))
    args = parser.parse_args()
    if args.action == "start":
        start()
    elif args.action == "init-db":
        init_database()
    elif args.action == "status":
        return 0 if status() else 1
    else:
        require_owner()
        if status():
            command("pg_ctl", "-D", str(DATA), "-w", "-m", "fast", "stop")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
