"""Cache only the owned original-task database schemas for isolated workers."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adapters import toolathlon_native as native
from workenv.core import no_links, read_json, save
from workenv.postgres_binary import postgres_binary


def shipping_configuration_snapshot():
    result = {}
    with native.database() as connection:
        with connection.cursor() as cursor:
            for table in ("shipping_zones", "shipping_zone_locations", "shipping_zone_methods"):
                cursor.execute("SELECT row_to_json(t)::text FROM wc." + table + " t ORDER BY row_to_json(t)::text LIMIT 100001")
                result[table] = [json.loads(row[0]) for row in cursor.fetchall()]
                if len(result[table]) > 100000: raise ValueError("Shipping configuration row budget")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", choices=(native.TASK_ID, "howtocook-meal-plan-gcal", "wc-order-analysis-gcal", "wc-order-fulfillment-tracker", "wc-shipping-zone-gsheet", "wc-customer-order-gsheet-email"), default=native.TASK_ID)
    args = parser.parse_args()
    if args.task_id == "wc-customer-order-gsheet-email":
        from integrations.vip_seed import prepare
        print(json.dumps({'prepared':True,'task_id':args.task_id,'sha256':prepare(ROOT)['sha256'],'training_sample':False}))
        return 0
    meal = args.task_id == "howtocook-meal-plan-gcal"
    order = args.task_id == "wc-order-analysis-gcal"
    shipping = args.task_id == "wc-shipping-zone-gsheet"
    fulfillment = args.task_id == "wc-order-fulfillment-tracker"
    pin = read_json(ROOT / "configs/mainline_sources.lock.json")["sources"]["toolathlon_gym"]["commit"]
    head = subprocess.run(["git", "-C", str(native.UPSTREAM), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    if head != pin: raise ValueError("Original task checkout differs from its source pin")
    with native.database(): pass
    shipping_configuration = shipping_configuration_snapshot() if shipping else None
    cache = ROOT / ".runtime/native-episodes/seed-cache"
    no_links(cache, ROOT)
    cache.mkdir(parents=True, exist_ok=True)
    temporary = cache / ("dump-" + uuid.uuid4().hex + ".sql")
    completed = subprocess.run([str(postgres_binary(ROOT, "pg_dump")), "--host=127.0.0.1", "--port=5432", "--username=eigent",
        "--dbname=toolathlon_gym", *(["--schema=wc", "--schema=gsheet", "--exclude-table-data=gsheet.*"] if (fulfillment or shipping) else ["--schema=wc", "--schema=gcal", "--exclude-table-data=gcal.*"] if order else ["--schema=gcal", "--schema-only"] if meal else ["--schema=wc", "--schema=gsheet"]),
        "--no-owner", "--no-privileges", "--file=" + str(temporary)],
        env=native.env(), capture_output=True, timeout=40)
    temporary.with_suffix(".stdout.txt").write_bytes(completed.stdout)
    temporary.with_suffix(".stderr.txt").write_bytes(completed.stderr)
    if completed.returncode or not 0 < temporary.stat().st_size <= 16 * 1024 * 1024:
        raise RuntimeError("Owned schema dump did not complete within its budget")
    raw = temporary.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    target = cache / (checksum + ".sql")
    no_links(target, ROOT)
    if target.exists():
        if target.read_bytes() != raw: raise ValueError("Immutable schema cache bytes changed")
    else:
        with target.open("xb") as stream: stream.write(raw)
    record = {"schema_version": "isolated-original-schema-seed-v1", "task_commit": pin,
              "seed_ref": target.relative_to(ROOT).as_posix(), "sha256": checksum, "bytes": len(raw),
              "schemas": ["wc", "gcal"] if order else ["gcal"] if meal else ["wc", "gsheet"], "execution_kind": "resource_seed_snapshot", "training_sample": False}
    if meal:
        if b"COPY gcal.events" in raw: raise ValueError("Meal task requires a structure-only calendar seed")
        from integrations.task_profiles import build_registry
        profile = build_registry(ROOT)["profiles"]["howtocook-meal-plan-gcal-v1"]
        record.update(task_id=args.task_id, schema_only=True, task_config_sha256=profile["task_config_sha256"])
    if order:
        if b"COPY gcal.events" in raw: raise ValueError("Order task seed must not include shared calendar rows")
        from integrations.task_profiles import build_registry
        profile = build_registry(ROOT)["profiles"]["wc-order-analysis-gcal-v1"]
        record.update(task_id=args.task_id, calendar_rows_included=False, task_config_sha256=profile["task_config_sha256"])
    if fulfillment or shipping:
        if b"COPY gsheet." in raw: raise ValueError("Fulfillment seed must not include shared sheet rows")
        from integrations.task_profiles import build_registry
        profile = build_registry(ROOT)["profiles"][args.task_id + "-v1"]
        record.update(task_id=args.task_id, sheet_rows_included=False, task_config_sha256=profile["task_config_sha256"])
    if shipping:
        if shipping_configuration_snapshot() != shipping_configuration:
            raise ValueError("Shipping configuration changed during seed dump")
        from integrations.shipping_components import configuration_digest
        record["shipping_configuration_sha256"] = configuration_digest(shipping_configuration)
    save(ROOT / (".runtime/nemo-vm/shipping-seed.json" if shipping else ".runtime/nemo-vm/fulfillment-seed.json" if fulfillment else ".runtime/nemo-vm/order-analysis-seed.json" if order else ".runtime/nemo-vm/meal-plan-seed.json" if meal else ".runtime/nemo-vm/isolated-seed.json"), record)
    print("Prepared owned isolated resource schema seed: " + checksum)
    return 0


if __name__ == "__main__": raise SystemExit(main())
