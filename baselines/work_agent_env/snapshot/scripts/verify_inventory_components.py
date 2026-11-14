"""Run original product/Calendar/PG-email tools in two owned inventory resources."""
import asyncio
from contextlib import ExitStack, AsyncExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from integrations import inventory_components as component
from integrations import inventory_data as data
from integrations.inventory_components_audit import CHECKS, SOURCES, audit
from integrations.episode_cluster import reserve_cluster, require_dependents_closed
from scripts.prepare_inventory_data_seed import shared_state, validate_seed
from workenv.core import ResourceError, save


def execute(cluster, sql, arguments=()):
    connection = cluster.connect()
    try:
        with connection, connection.cursor() as cursor: cursor.execute(sql, arguments)
    finally: connection.close()


def main():
    output = ROOT / "evidence/mainline" / ("inventory-components-" + uuid4().hex); output.mkdir()
    checks, clusters, controllers, measurements = [], [], [], []
    ledgers = {n: (ROOT / ".runtime/nemo-vm" / n).read_bytes() for n in ("resource-sessions.json", "isolated-sessions.json", "synthetic-sessions.json")}
    for n, raw in ledgers.items(): (output / ("ledger-before-" + n)).write_bytes(raw)
    before = shared_state(ROOT); after = None; error = None
    build = json.loads((ROOT / "evidence/mainline/email_build.json").read_text())
    def check(index, value):
        checks.append({"name": CHECKS[index], "passed": bool(value)})
        if not value: raise AssertionError(CHECKS[index])
    def measure(name, index):
        result = controllers[index].evaluate()
        measurements.append({"name": name, "slot": index, "evaluation_ref": result["evaluation_ref"]})
        return result
    async def run():
        seed = json.loads((ROOT / ".runtime/nemo-vm/inventory-data-seed.json").read_text()); sql = validate_seed(ROOT, seed)
        async def close(context, controller):
            if controller.opened: await context.__aexit__(None, None, None)
        with ExitStack() as resources:
            for slot in (0, 1):
                cluster = resources.enter_context(reserve_cluster(ROOT, slot=slot)); cluster.start(sql); clusters.append(cluster)
            async with AsyncExitStack() as services:
                controllers.extend([None, None]); contexts = [None, None]
                for i in (1, 0):
                    c = component.InventoryComponents(ROOT, clusters[i], build)
                    context = c.open(); await context.__aenter__()
                    services.push_async_callback(close, context, c); controllers[i] = c; contexts[i] = context
                check(0, all(len(c.children) == 3 and len(c.schemas) == 7 for c in controllers))
                truths = [await c.collect_sources() for c in controllers]
                check(1, all(len(t["products"]) == 5 and t["products"] == c.initial["sql_zero_stock"] for t, c in zip(truths, controllers)))
                event_args, event_rids, mail_rids = [], [], []
                for i, (c, truth) in enumerate(zip(controllers, truths)):
                    args_for_resource, rids = [], []
                    for event in truth["events"]:
                        args = {"summary": event["summary"], "description": event["description"],
                            "start": {"dateTime": event["start_datetime"], "timeZone": event["start_timezone"]},
                            "end": {"dateTime": event["end_datetime"], "timeZone": event["end_timezone"]}}
                        rid = str(uuid4()); await c.call("google_calendar", "create_event", args, rid)
                        args_for_resource.append(args); rids.append(rid)
                    event_args.append(args_for_resource); event_rids.append(rids)
                    state = data.snapshot(ROOT, c.cluster)
                    await c.call("google_calendar", "get_event", {"eventId": state["events"][0]["id"]}, str(uuid4()))
                    await c.call("google_calendar", "list_events", {"timeMin": "2026-03-10T00:00:00Z", "timeMax": "2026-03-11T00:00:00Z", "maxResults": 100}, str(uuid4()))
                    rid = str(uuid4()); mail_rids.append(rid)
                    await c.call("email", "send_email", {"to": data.validation.RECIPIENT, "subject": data.validation.SUBJECT, "body": truth["email_body"]}, rid)
                    c.write_report(truth["report_text"])
                check(2, all(measure("positive", i)["component_measurement_passed"] is True for i in (0, 1)))
                initial = data.snapshot(ROOT, clusters[0])
                c = controllers[0]
                await c.call("google_calendar", "create_event", event_args[0][0], event_rids[0][0])
                await c.call("email", "send_email", {"to": data.validation.RECIPIENT, "subject": data.validation.SUBJECT, "body": truths[0]["email_body"]}, mail_rids[0])
                check(3, data.snapshot(ROOT, clusters[0]) == initial)
                refused = 0
                for server, name, args, rid in (("google_calendar", "create_event", {**event_args[0][0], "summary": "conflict"}, event_rids[0][0]),
                    ("google_calendar", "get_event", {"eventId": data.snapshot(ROOT, clusters[1])["events"][0]["id"]}, str(uuid4()))):
                    try: await c.call(server, name, args, rid)
                    except (ResourceError, ValueError): refused += 1
                check(4, refused == 2 and data.snapshot(ROOT, clusters[0]) == initial)
                c.write_report(truths[0]["report_text"].replace(truths[0]["products"][0]["name"], "WRONG NAME", 1))
                result = measure("wrong_report", 0); check(5, result["original_passed"] is True and result["component_measurement_passed"] is False)
                c.write_report(truths[0]["report_text"])
                execute(clusters[0], "UPDATE gcal.events SET start_datetime='2026-03-10T13:30:00Z'")
                result = measure("wrong_calendar", 0); check(6, result["original_passed"] is True and result["component_measurement_passed"] is False)
                execute(clusters[0], "UPDATE gcal.events SET start_datetime=%s", (data.validation.START.isoformat(),))
                check(7, measure("restored", 0)["component_measurement_passed"] is True)
                fault_rid = str(uuid4()); second = controllers[1]
                execute(clusters[1], "ALTER TABLE email.sent_log ADD CONSTRAINT inventory_partial_send CHECK (false) NOT VALID")
                try:
                    try: await second.call("email", "send_email", {"to": data.validation.RECIPIENT, "subject": data.validation.SUBJECT, "body": truths[1]["email_body"]}, fault_rid)
                    except ResourceError: unknown = True
                    else: unknown = False
                finally: execute(clusters[1], "ALTER TABLE email.sent_log DROP CONSTRAINT inventory_partial_send")
                partial = data.snapshot(ROOT, clusters[1])
                check(8, unknown and second.unknown and len(partial["email"]["messages"]) == 2 and len(partial["email"]["sent_log"]) == 1)
                blocked = 0
                for rid in (fault_rid, str(uuid4())):
                    try: await second.call("email", "send_email", {"to": data.validation.RECIPIENT, "subject": data.validation.SUBJECT, "body": truths[1]["email_body"]}, rid)
                    except ResourceError: blocked += 1
                try: second.evaluate()
                except ResourceError: blocked += 1
                await contexts[0].__aexit__(None, None, None)
                report = data.artifacts.read_report(ROOT, clusters[0])["text"]
                reset = data.initialize(ROOT, clusters[0], resetting=True)
                save(output / "reset_snapshots.json", {"first": reset, "second_before": partial, "second_after": data.snapshot(ROOT, clusters[1])})
                check(10, data.snapshot(ROOT, clusters[1]) == partial and data.artifacts.read_report(ROOT, clusters[0])["text"] == report)
            try: data.initialize(ROOT, clusters[1], resetting=True)
            except ResourceError: blocked += 1
            check(9, blocked == 4 and data.snapshot(ROOT, clusters[1]) == partial)
            for cluster in clusters: require_dependents_closed(cluster.profile)
        check(11, all(all(child.get("closed") for child in c.children.values()) for c in controllers)
            and all(not (c.profile.data / "postmaster.pid").exists() for c in clusters))
    try:
        asyncio.run(run()); after = shared_state(ROOT)
        check(12, before == after and all((ROOT / ".runtime/nemo-vm" / n).read_bytes() == raw for n, raw in ledgers.items()))
    except BaseException as exc: error = type(exc).__name__ + ": " + str(exc)[:600]
    finally:
        for n in ledgers: (output / ("ledger-after-" + n)).write_bytes((ROOT / ".runtime/nemo-vm" / n).read_bytes())
        retained = {}
        for base in [output, *[c.profile.base for c in clusters]]:
            for path in base.rglob("*"):
                if path.is_file() and "pgdata" not in path.parts and path.suffix in (".json", ".txt", ".py", ".mjs"):
                    retained[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        # Checks 9 and 10 are observed in safe context-manager closing order.
        checks.sort(key=lambda row: CHECKS.index(row["name"]))
        record = {"schema_version": "inventory-original-components-v1", "at": datetime.now(timezone.utc).isoformat(),
            "passed": error is None and len(checks) == len(CHECKS), "error": error, "checks": checks,
            "profiles": [c.profile.record() for c in clusters], "measurements": measurements, "retained_sha256": retained,
            "source_sha256": {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in SOURCES},
            "email_build": build, "calendar_build": json.loads((ROOT / "evidence/mainline/calendar_build.json").read_text()),
            "shared_before": before, "shared_after": after,
            "ledgers_before": {n: hashlib.sha256(raw).hexdigest() for n, raw in ledgers.items()},
            "ledgers_after": {n: hashlib.sha256((ROOT / ".runtime/nemo-vm" / n).read_bytes()).hexdigest() for n in ledgers},
            "fixture_mode": "original_woocommerce_calendar_pg_email_tools", "nemo_task_executed": False, "runtime_activation": False,
            "training_admission": False, "reference_files_read": False, "official_benchmark_score_available": False,
            "hard_interruption_verified": False, "real_model_calls": 0, "training_updates": 0, "external_messages_sent": 0}
        save(output / "result.json", record)
    if error:
        print(json.dumps({"passed": False, "error": error, "receipt": str(output / "result.json")})); return 1
    print(json.dumps({"passed": True, "receipt": str(output / "result.json"), "audit": audit(ROOT, output / "result.json")})); return 0


if __name__ == "__main__": raise SystemExit(main())
