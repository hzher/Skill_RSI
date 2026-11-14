"""Real isolated PG email tools and partial-write diagnostics; no external mail."""
import asyncio
from contextlib import ExitStack, AsyncExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adapters import toolathlon_native as native
from integrations.email_build import verify_build
from integrations.email_components import EmailComponents, initialize, snapshot as email_snapshot, TABLES
from integrations.episode_cluster import reserve_cluster, require_dependents_closed
from scripts.verify_calendar_isolation import shared_snapshot
from training.email_task_review import PIN, BASE, public_blob
from integrations.email_task_identity import EmailTaskIdentity
from training.inventory_task_review import TASK_ID as INVENTORY_TASK
from scripts.prepare_inventory_data_seed import validate_seed
from workenv.core import ResourceError, contained, save

from integrations.email_audit import CHECKS, SOURCES


def shared_email_digest():
    values = {}
    with native.database() as connection:
        with connection.cursor() as cursor:
            for table in TABLES:
                cursor.execute("SELECT row_to_json(t)::text FROM email." + table + " t ORDER BY row_to_json(t)::text LIMIT 10001")
                values[table] = [r[0] for r in cursor.fetchall()]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def main():
    tasks = [EmailTaskIdentity().task_id, INVENTORY_TASK]
    def snapshot(cluster):
        return email_snapshot(cluster, task_id=tasks[cluster.profile.slot])
    output = ROOT / "evidence/mainline" / ("email-components-" + uuid4().hex); output.mkdir()
    checks, clusters, controllers, retained, fault = [], [], [], {}, {}
    build = json.loads((ROOT / "evidence/mainline/email_build.json").read_text())
    seed = json.loads((ROOT / ".runtime/nemo-vm/email-component-seed.json").read_text())
    _, config = public_blob(ROOT, BASE + "task_config.json")
    verify_build(ROOT, build, check_environment=True)
    sql = contained(ROOT, seed["seed_ref"])
    inventory_seed = json.loads((ROOT / ".runtime/nemo-vm/inventory-data-seed.json").read_text())
    inventory_sql = validate_seed(ROOT, inventory_seed)
    before = {"office": shared_snapshot(), "email_sha256": shared_email_digest()}
    ledgers = {n: (ROOT / ".runtime/nemo-vm" / n).read_bytes() for n in ("resource-sessions.json", "isolated-sessions.json", "synthetic-sessions.json")}
    for name, raw in ledgers.items(): (output / ("ledger-before-" + name)).write_bytes(raw)
    after, error = None, None
    def check(name, passed):
        checks.append({"name": name, "passed": bool(passed)})
        if not passed: raise AssertionError(name)
    async def call(i, name, args, rid=None): return await controllers[i].call(name, args, rid or str(uuid4()))
    def message(i, body="Thank you for your loyalty."):
        return {"to": "customer" + str(i) + "@store.example.com", "subject": "Local PG diagnostic", "body": body}
    def execute(cluster, statement):
        connection = cluster.connect()
        try:
            connection.rollback()
            connection.autocommit = True
            with connection.cursor() as cursor: cursor.execute(statement)
        finally: connection.close()
    async def run():
        async def close_open(context, controller):
            if controller.opened: await context.__aexit__(None, None, None)
        check(CHECKS[0], seed["source_commit"] == PIN and seed["task_config_sha256"] == config["sha256"]
            and seed["schemas"] == ["wc", "gsheet", "email"] and seed["sheets_and_email_rows_included"] is False
            and hashlib.sha256(sql.read_bytes()).hexdigest() == seed["sha256"])
        with ExitStack() as owned:
            for slot in (0, 1):
                cluster = owned.enter_context(reserve_cluster(ROOT, slot=slot, resource_id=str(uuid4())))
                cluster.start(sql if slot == 0 else inventory_sql); clusters.append(cluster)
            async with AsyncExitStack() as services:
                contexts = [None, None]; controllers.extend([None, None])
                # Exit the most recently entered MCP cancel scope first.
                for i in (1, 0):
                    c = clusters[i]
                    component = EmailComponents(ROOT, c, build) if i == 0 else EmailComponents(ROOT, c, build, task_id=tasks[i])
                    context = component.open(); controller = await context.__aenter__()
                    services.push_async_callback(close_open, context, controller)
                    contexts[i] = context; controllers[i] = controller
                initial = [snapshot(c) for c in clusters]
                save(output / "initial_snapshots.json", initial)
                check(CHECKS[1], all(not any(d["email"].values()) and not d["office"].get("cells", []) and not d["office"].get("events", []) for d in initial))
                check(CHECKS[2], all({t["name"] for t in c.schemas} == {"send_email", "get_emails", "read_email"} for c in controllers))
                observations = [json.loads((c.run / "email-process.json").read_text()) for c in controllers]
                check(CHECKS[3], all(o["forbidden_socket_blocked"] is True and o["methods"] == ["send_email", "get_emails", "read_email"]
                    and o["backend_classes"] == ["emails_mcp.backends.pg_backend.PgIMAPBackend", "emails_mcp.backends.pg_backend.PgSMTPBackend"] for o in observations))
                rid = str(uuid4())
                first = await call(0, "send_email", message(0), rid)
                await call(1, "send_email", message(1))
                check(CHECKS[4], all(len(snapshot(c)["email"]["messages"]) == 1 and len(snapshot(c)["email"]["sent_log"]) == 1
                    and snapshot(c)["email"]["messages"][0]["from_addr"] == EmailTaskIdentity(tasks[c.profile.slot]).sender for c in clusters))
                before_replay = snapshot(clusters[0]); second = await call(0, "send_email", message(0), rid)
                check(CHECKS[5], first == second and snapshot(clusters[0]) == before_replay)
                try: await call(0, "send_email", message(0, "changed"), rid)
                except ValueError: conflict = True
                else: conflict = False
                check(CHECKS[6], conflict and snapshot(clusters[0]) == before_replay)
                for name, args in (("delete_email", {"email_id": "1"}), ("send_email", {**message(0), "attachments": ["/private"]})):
                    try: await call(0, name, args)
                    except ValueError: pass
                    else: raise AssertionError("Unallowlisted email operation accepted")
                check(CHECKS[7], snapshot(clusters[0]) == before_replay)
                await call(0, "get_emails", {"folder": "Sent", "page": 1, "page_size": 20})
                mid = snapshot(clusters[0])["email"]["messages"][0]["id"]
                execute(clusters[0], "UPDATE email.messages SET is_read=FALSE WHERE id=" + str(int(mid)))
                await call(0, "read_email", {"email_id": str(mid)})
                check(CHECKS[8], snapshot(clusters[0])["email"]["messages"][0]["is_read"] is True)
                chinese = "\u611f\u8c22\u60a8\u7684\u652f\u6301\u3002\n\u672c\u5730 PG \u8bb0\u5f55 \U0001f4e7"
                await call(0, "send_email", message(0, chinese))
                check(CHECKS[9], any(r["body_text"] == chinese for r in snapshot(clusters[0])["email"]["messages"]))
                marker = clusters[0].profile.base / "email-initialized.json"; original = marker.read_bytes()
                data = json.loads(original); data["profile"] = {}; marker.write_text(json.dumps(data))
                try:
                    try: await call(0, "send_email", message(0))
                    except ResourceError: refused = True
                    else: refused = False
                finally: marker.write_bytes(original)
                check(CHECKS[10], refused and len(snapshot(clusters[0])["email"]["messages"]) == 2)
                await contexts[0].__aexit__(None, None, None)
                await call(1, "get_emails", {"folder": "Sent", "page": 1, "page_size": 20})
                check(CHECKS[11], controllers[0].child["closed"] and controllers[1].opened)
                second_before = snapshot(clusters[1]); reset = initialize(ROOT, clusters[0])
                save(output / "reset_snapshots.json", [reset, snapshot(clusters[1])])
                check(CHECKS[12], not reset["email"]["messages"] and snapshot(clusters[1]) == second_before
                    and all(snapshot(c)["office"]["readonly_sha256"] == d["office"]["readonly_sha256"] for c, d in zip(clusters, initial)))
                # The original PG backend autocommits multiple statements. Force
                # the last one to fail and retain the actual partial message.
                fault_id = str(uuid4()); fault["request_id"] = fault_id
                execute(clusters[1], "ALTER TABLE email.sent_log ADD CONSTRAINT workenv_partial_send CHECK (false) NOT VALID")
                try:
                    try: await call(1, "send_email", message(1, "partial-write-fixture"), fault_id)
                    except ResourceError: unknown = True
                    else: unknown = False
                finally: execute(clusters[1], "ALTER TABLE email.sent_log DROP CONSTRAINT workenv_partial_send")
                fault["after"] = snapshot(clusters[1]); save(output / "partial_write_snapshot.json", fault["after"])
                check(CHECKS[13], unknown and controllers[1].unknown and len(fault["after"]["email"]["messages"]) == 2
                    and len(fault["after"]["email"]["sent_log"]) == 1)
                for identity in (fault_id, str(uuid4())):
                    try: await call(1, "send_email", message(1, "partial-write-fixture"), identity)
                    except ResourceError: pass
                    else: raise AssertionError("Unknown email request replayed")
                check(CHECKS[14], snapshot(clusters[1]) == fault["after"])
            try: initialize(ROOT, clusters[1])
            except ResourceError: refused_reset = True
            else: refused_reset = False
            check(CHECKS[15], refused_reset and snapshot(clusters[1]) == fault["after"])
            for c in clusters: require_dependents_closed(c.profile)
            check(CHECKS[16], all(c.child["closed"] for c in controllers))
        check(CHECKS[17], all(not (c.profile.data / "postmaster.pid").exists() for c in clusters))
    try:
        asyncio.run(run())
        after = {"office": shared_snapshot(), "email_sha256": shared_email_digest()}
        check(CHECKS[18], before == after and all((ROOT / ".runtime/nemo-vm" / n).read_bytes() == raw for n, raw in ledgers.items()))
    except BaseException as exc:
        error = type(exc).__name__ + ": " + str(exc)[:400]
        raise
    finally:
        for name in ledgers:
            (output / ("ledger-after-" + name)).write_bytes((ROOT / ".runtime/nemo-vm" / name).read_bytes())
        for c in clusters:
            for p in c.profile.base.rglob("*"):
                if p.is_file() and "pgdata" not in p.parts and p.suffix in (".json", ".txt", ".py"):
                    retained[p.relative_to(ROOT).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        for p in output.iterdir():
            if p.is_file(): retained[p.relative_to(ROOT).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        record = {"schema_version": "owned-email-components-diagnostic-v1", "at": datetime.now(timezone.utc).isoformat(),
            "passed": error is None and [c["name"] for c in checks] == list(CHECKS) and all(c["passed"] for c in checks),
                "checks": checks, "error": error, "profiles": [c.profile.record() for c in clusters], "task_ids": tasks,
            "shared_before": before, "shared_after": after, "retained_sha256": retained,
            "ledger_sha256_before": {n: hashlib.sha256(raw).hexdigest() for n, raw in ledgers.items()},
            "ledger_sha256_after": {n: hashlib.sha256((ROOT / ".runtime/nemo-vm" / n).read_bytes()).hexdigest() for n in ledgers},
            "source_sha256": {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in SOURCES},
            "build": build, "runtime_task_activated": False, "task_passed": None, "reward": None,
            "real_model_calls": 0, "training_updates": 0, "training_admissions": 0, "external_messages_sent": 0}
        save(output / "result.json", record)
        print(json.dumps({"passed": record["passed"], "checks": len(checks), "receipt": (output / "result.json").relative_to(ROOT).as_posix()}))


if __name__ == "__main__": main()
