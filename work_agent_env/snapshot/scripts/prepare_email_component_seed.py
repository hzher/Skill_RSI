"""Prepare an immutable wc plus empty Sheets/email seed for local components only."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adapters import toolathlon_native as native
from integrations.email_components import TABLES
from training.email_task_review import PIN, BASE, public_blob
from workenv.core import no_links, save
from workenv.postgres_binary import postgres_binary


def main():
    _, config = public_blob(ROOT, BASE + "task_config.json")
    with native.database() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='email'")
            if {r[0] for r in cursor.fetchall()} != set(TABLES): raise ValueError("Owned email schema is incomplete")
    cache = ROOT / ".runtime/native-episodes/seed-cache"; no_links(cache, ROOT); cache.mkdir(parents=True, exist_ok=True)
    temporary = cache / ("email-dump-" + uuid4().hex + ".sql")
    result = subprocess.run([str(postgres_binary(ROOT, "pg_dump")), "--host=127.0.0.1", "--port=5432", "--username=eigent",
        "--dbname=toolathlon_gym", "--schema=wc", "--schema=gsheet", "--schema=email", "--exclude-table-data=gsheet.*",
        "--exclude-table-data=email.*", "--no-owner", "--no-privileges", "--file=" + str(temporary)],
        env=native.env(), capture_output=True, timeout=40)
    temporary.with_suffix(".stdout.txt").write_bytes(result.stdout); temporary.with_suffix(".stderr.txt").write_bytes(result.stderr)
    if result.returncode or not 0 < temporary.stat().st_size <= 16 * 1024 * 1024: raise RuntimeError("Owned email schema dump failed")
    raw = temporary.read_bytes()
    if b"COPY email." in raw or b"COPY gsheet." in raw: raise ValueError("Email component seed includes shared writable rows")
    checksum = hashlib.sha256(raw).hexdigest(); target = cache / (checksum + ".sql")
    if target.exists() and target.read_bytes() != raw: raise ValueError("Immutable email seed changed")
    if not target.exists():
        with target.open("xb") as stream: stream.write(raw)
    record = {"schema_version": "owned-email-component-seed-v1", "task_id": "wc-customer-order-gsheet-email",
        "source_commit": PIN, "task_config_sha256": config["sha256"], "schemas": ["wc", "gsheet", "email"],
        "sheets_and_email_rows_included": False, "seed_ref": target.relative_to(ROOT).as_posix(), "sha256": checksum,
        "runtime_activation": False, "training_admission": False}
    save(ROOT / ".runtime/nemo-vm/email-component-seed.json", record)
    print(json.dumps({"prepared": True, "runtime_activation": False, "sha256": checksum}))


if __name__ == "__main__": main()
