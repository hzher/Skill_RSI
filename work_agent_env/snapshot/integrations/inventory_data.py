"""Owned inventory initialization, independent SQL and original product paging.

This component does not launch email/Calendar tools, run an evaluator or register
a NeMo task. Its reset scope is the pinned database preprocess, not file reset.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from adapters import inventory_alert_validation as validation
from adapters import inventory_alert_artifacts as artifacts
from integrations.episode_cluster import require_dependents_closed
from training.inventory_task_review import TASK_ID, public_blob
from workenv.core import ResourceError, no_links, save

EMAIL_TABLES = ("account_config", "attachments", "drafts", "folders", "messages", "sent_log")
RESET_TABLES = ("attachments", "drafts", "messages", "sent_log")
SCHEMAS = ("wc", "gcal", "email")
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_PAGES = 64
PAGE_SIZE = 10
ZERO_STOCK_SQL = """SELECT id,name,sku,
CASE WHEN jsonb_array_length(categories)=0 THEN 'Uncategorized'
     ELSE categories->0->>'name' END AS category
FROM wc.products WHERE stock_quantity=0 ORDER BY id"""


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def row_summary(rows):
    ordered=sorted(json.dumps(row,sort_keys=True,ensure_ascii=False,allow_nan=False) for row in rows)
    return {"count":len(rows),"sha256":checksum(ordered)}


def require_cluster(root, cluster):
    if cluster.profile.owner != Path(root).resolve() or not cluster.started:
        raise ResourceError("Inventory cluster is not owned and active")
    cluster.require_owner()


def catalog_projection(products):
    # Validate the complete input before projecting provider-specific fields.
    validation.expected(products, source_binding=validation.source_identity())
    return sorted([{"id": validation.integer(p["id"], minimum=1), "name": p["name"], "sku": p["sku"],
        "stock_quantity": int(validation.number(p["stock_quantity"])),
        "categories": [c["name"] for c in p["categories"]]} for p in products], key=lambda p:p["id"])


def validate_snapshot(data):
    if not isinstance(data, dict) or not {"products", "events", "email", "readonly_tables", "readonly_sha256", "sql_zero_stock"} <= set(data):
        raise ResourceError("Inventory snapshot fields missing")
    truth = validation.expected(data["products"], source_binding=validation.source_identity())
    if data["sql_zero_stock"] != truth["products"]:
        raise ResourceError("Independent SQL and Python zero-stock truth differ")
    if not isinstance(data["readonly_tables"], dict) or "wc.products" not in data["readonly_tables"]:
        raise ResourceError("Inventory read-only table summary missing")
    if checksum(data["readonly_tables"]) != data["readonly_sha256"]:
        raise ResourceError("Inventory read-only table summary changed")
    if data["readonly_tables"]["wc.products"] != row_summary(data["products"]):
        raise ResourceError("Inventory product rows do not match the read-only table digest")
    if not isinstance(data["email"], dict) or set(data["email"]) != set(EMAIL_TABLES) or not isinstance(data["events"], list):
        raise ResourceError("Inventory writable snapshot shape differs")
    return truth


def snapshot(root, cluster):
    require_cluster(root, cluster)
    result = {"email": {}, "readonly_tables": {}}
    connection = cluster.connect()
    try:
        connection.rollback(); connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='wc' AND table_type='BASE TABLE' ORDER BY table_name")
            tables = [r[0] for r in cursor.fetchall()]
            if "products" not in tables or not 1 <= len(tables) <= 64 or any(not re.fullmatch(r"[a-z_][a-z0-9_]*",t) for t in tables):
                raise ResourceError("Inventory read-only schema inventory differs")
            for table in tables:
                cursor.execute("SELECT row_to_json(t)::text FROM wc." + table + " t ORDER BY row_to_json(t)::text LIMIT 100001")
                rows = [json.loads(r[0]) for r in cursor.fetchall()]
                if len(rows) > 100000: raise ResourceError("Inventory table row budget")
                result["readonly_tables"]["wc." + table] = row_summary(rows)
                if table == "products": result["products"] = sorted(rows, key=lambda p:p["id"])
            for key, table in [("events", "gcal.events"), *[(n, "email." + n) for n in EMAIL_TABLES]]:
                cursor.execute("SELECT row_to_json(t)::text FROM " + table + " t ORDER BY row_to_json(t)::text LIMIT 1025")
                rows = [json.loads(r[0]) for r in cursor.fetchall()]
                if len(rows) > 1024: raise ResourceError("Inventory writable row budget")
                if key == "events": result[key] = rows
                else: result["email"][key] = rows
            cursor.execute(ZERO_STOCK_SQL)
            result["sql_zero_stock"] = [dict(zip(("id", "name", "sku", "category"), row)) for row in cursor.fetchall()]
    finally:
        connection.close()
    result["readonly_sha256"] = checksum(result["readonly_tables"])
    if len(json.dumps(result).encode()) > MAX_SNAPSHOT_BYTES:
        raise ResourceError("Inventory snapshot byte budget")
    validate_snapshot(result)
    return result


def initialize(root, cluster, *, resetting=False):
    root=Path(root).resolve(); require_cluster(root, cluster); require_dependents_closed(cluster.profile)
    from integrations.email_task_identity import EmailTaskIdentity, require_resolved_requests
    require_resolved_requests(root, cluster, EmailTaskIdentity(TASK_ID))
    from integrations.email_task_identity import _read
    journal_path=cluster.profile.base/'inventory-components-run/requests.json'
    if journal_path.exists():
        journal,_=_read(root,journal_path,limit=32*1024*1024)
        entries=journal.get('entries')
        if (journal.get('schema_version')!='inventory-original-request-journal-v1' or journal.get('task_id')!=TASK_ID
            or journal.get('profile')!=cluster.profile.record() or type(entries) is not dict or len(entries)>128
            or any(type(e) is not dict or e.get('state')!='committed' for e in entries.values())):
            raise ResourceError('Inventory combined request unresolved; reset refused')
    workspace=cluster.profile.base/"inventory-workspace"; no_links(workspace,root); workspace.mkdir(exist_ok=True)
    target=workspace/validation.FILENAME
    artifacts.require_no_pending(root,cluster,target)
    if not resetting and target.exists():
        raise ResourceError("Inventory initial workspace already has a report")
    marker=cluster.profile.base/"inventory-initialized.json"
    record={"task_id":TASK_ID,"profile":cluster.profile.record(),"contract_source":validation.source_identity()}
    save(marker,{**record,"state":"initializing"})
    before=snapshot(root,cluster)
    raw,identity=public_blob(root,"preprocess/main.py")
    run=cluster.profile.base/("inventory-preprocess-"+uuid4().hex); run.mkdir()
    script=run/"original_preprocess.py"; script.write_bytes(raw)
    result=subprocess.run([sys.executable,str(script),"--agent_workspace",str(workspace)],cwd=run,
        env=cluster.profile.environment(),capture_output=True,timeout=25)
    (run/"stdout.txt").write_bytes(result.stdout); (run/"stderr.txt").write_bytes(result.stderr)
    save(run/"command.json",{"exit_code":result.returncode,"profile":cluster.profile.record(),"source":identity})
    after=snapshot(root,cluster)
    if (result.returncode or after["events"] or any(after["email"][n] for n in RESET_TABLES)
        or before["readonly_sha256"]!=after["readonly_sha256"]
        or any(before["email"][n]!=after["email"][n] for n in ("account_config","folders"))):
        raise ResourceError("Inventory original preprocess failed or changed unrelated rows")
    save(marker,{**record,"state":"ready","preprocess_source":identity,
        "preprocess_ref":run.relative_to(root).as_posix(),"readonly_sha256":after["readonly_sha256"],
        "reset_scope":"original_database_preprocess_only_report_file_preserved"})
    return after


def decode_products(response):
    if not isinstance(response,dict) or response.get("isError"):
        raise ResourceError("Original product response is an error")
    try:
        raw="".join(b["text"] for b in response["content"] if b["type"]=="text")
        if len(raw.encode())>4*1024*1024: raise ValueError("Response byte budget")
        rows=json.loads(raw)
    except (KeyError,TypeError,ValueError,UnicodeError) as exc:
        raise ResourceError("Original product JSON is unavailable") from exc
    if not isinstance(rows,list) or len(rows)>PAGE_SIZE:
        raise ResourceError("Original product page shape differs")
    return rows


async def collect_products(call, expected_products):
    """call(name, args) is the original MCP; no write retry or hidden fallback."""
    rows,journal=[],[]
    for page in range(1,MAX_PAGES+1):
        args={"page":page,"perPage":PAGE_SIZE}
        response=await call("woo_products_list",args)
        batch=decode_products(response)
        journal.append({"name":"woo_products_list","arguments":args,"response":response})
        rows.extend(batch)
        # Fail on duplicates/invalid source fields before requesting later pages.
        catalog_projection(rows)
        if len(batch)<PAGE_SIZE:break
    else:
        raise ResourceError("Original product pagination did not terminate")
    if catalog_projection(rows)!=catalog_projection(expected_products):
        raise ResourceError("Original product pagination omitted or changed SQL catalog rows")
    return rows,journal


def audit_pages(journal, expected_products):
    if not isinstance(journal,list) or not 1<=len(journal)<=MAX_PAGES:
        raise ValueError("Inventory page journal budget")
    rows=[]
    for index,entry in enumerate(journal):
        if entry.get("name")!="woo_products_list" or entry.get("arguments")!={"page":index+1,"perPage":PAGE_SIZE}:
            raise ValueError("Inventory original page sequence rebound")
        batch=decode_products(entry["response"])
        if (index<len(journal)-1 and len(batch)!=PAGE_SIZE) or (index==len(journal)-1 and len(batch)>=PAGE_SIZE):
            raise ValueError("Inventory pagination terminal condition differs")
        rows.extend(batch)
    if catalog_projection(rows)!=catalog_projection(expected_products):
        raise ValueError("Inventory pages differ from complete SQL source")
    return {"pages":len(journal),"products":len(rows)}
