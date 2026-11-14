"""Exercise the unchanged original Calendar MCP against two owned PG episodes.

Only the shared gcal schema is copied; no seed events, task answers, model calls,
NeMo task activation or training admissions are part of this component probe.
"""
import asyncio
from contextlib import AsyncExitStack, ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adapters import toolathlon_native as native
from integrations.calendar_component import verify_build, initialize, calendar_session, require_schema
from integrations.calendar_audit import CHECKS
from integrations.episode_cluster import reserve_cluster
from workenv.core import save
from workenv.postgres_binary import postgres_binary

SOURCES = ("scripts/verify_calendar_isolation.py", "integrations/calendar_component.py", "integrations/episode_cluster.py",
           "integrations/calendar_audit.py", "adapters/toolathlon_native.py", "workenv/process_identity.py")


def query(cluster, sql, values=()):
    conn = cluster.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, values)
            return cur.fetchall() if cur.description else None
    finally: conn.close()


def shared_snapshot():
    with native.database() as conn, conn.cursor() as cur:
        result = {}
        for table in (*native.TABLES, "gcal.events"):
            cur.execute("SELECT count(*),md5(coalesce(string_agg(row_to_json(t)::text,E'\\n' ORDER BY row_to_json(t)::text),'')) FROM " + table + " t")
            count, digest = cur.fetchone()
            result[table] = {"count": count, "database_row_digest_md5": digest}
        return result


def main():
    output = ROOT / "evidence/mainline" / ("calendar-probe-" + uuid.uuid4().hex)
    output.mkdir(parents=True)
    private = ROOT / ".runtime/native-episodes" / output.name
    private.mkdir(parents=True)
    seed = private / "calendar-schema.sql"
    build = json.loads((ROOT / "evidence/mainline/calendar_build.json").read_text(encoding="utf-8"))
    verify_build(ROOT, build)
    checks, journal, profiles, sessions, retained, clusters = [], [], [], [], {}, []
    before, after = None, None
    observations = {}
    def check(name, value):
        checks.append({"name": name, "passed": bool(value)})
        if not value: raise AssertionError(name)
    def retain(path): retained[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    async def call(client, index, name, arguments):
        reply = await client.call_tool(name, arguments)
        journal.append({"episode": profiles[index]["resource_id"], "slot": index, "name": name,
                        "arguments": arguments, "response": reply.model_dump(mode="json")})
        return reply
    def content(reply):
        if reply.isError: raise AssertionError("Original calendar tool failed: " + reply.content[0].text)
        return reply.content[0].text
    def event(reply): return json.loads(content(reply))
    def event_id(reply):
        match = re.match(r"Event created with ID: ([0-9a-f-]+)\n", content(reply))
        if not match or str(uuid.UUID(match[1])) != match[1]: raise ValueError("Original calendar returned an invalid event ID")
        return match[1]
    try:
        before = shared_snapshot()  # Includes dedicated data-directory attestation.
        completed = subprocess.run([str(postgres_binary(ROOT, "pg_dump")), "--host=127.0.0.1", "--port=5432",
            "--username=eigent", "--dbname=toolathlon_gym", "--schema=gcal", "--schema-only", "--no-owner", "--no-privileges", "--file=" + str(seed)],
            env=native.env(), capture_output=True, timeout=30)
        (private / "dump.stdout.txt").write_bytes(completed.stdout)
        (private / "dump.stderr.txt").write_bytes(completed.stderr)
        check(CHECKS[0], completed.returncode == 0 and 0 < seed.stat().st_size < 256 * 1024
              and b"CREATE TABLE gcal.events" in seed.read_bytes() and b"COPY gcal.events" not in seed.read_bytes())
        retain(seed)
        with ExitStack() as stack:
            clusters = [stack.enter_context(reserve_cluster(ROOT, slot=i)) for i in (0, 1)]
            workspaces = []
            for cluster in clusters:
                cluster.start(seed)
                profiles.append(cluster.profile.record())
                workspaces.append(initialize(ROOT, build, cluster))
            check(CHECKS[1], len({p["host"] for p in profiles}) == 2 and len({p["data_directory"] for p in profiles}) == 2)
            check(CHECKS[2], all(require_schema(c) == 0 for c in clusters))
            observations["initialized_counts"] = [require_schema(c) for c in clusters]
            async def exercise():
                async with AsyncExitStack() as clients_stack:
                    clients = []
                    for i, cluster in enumerate(clusters):
                        err = stack.enter_context((output / ("server-" + str(i) + ".stderr.txt")).open("w", encoding="utf-8"))
                        client, record = await clients_stack.enter_async_context(calendar_session(ROOT, build, cluster, err))
                        clients.append(client)
                        sessions.append(record)
                    schemas = [await client.list_tools() for client in clients]
                    save(output / "tool_schemas.json", [value.model_dump(mode="json") for value in schemas])
                    retain(output / "tool_schemas.json")
                    check(CHECKS[3], all({tool.name for tool in schema.tools} == {"create_event", "get_event", "update_event", "delete_event", "list_events"} for schema in schemas))
                    ids, dst_ids, records, dst_records = [], [], [], []
                    for i, client in enumerate(clients):
                        args = {"summary": "Component diagnostic slot " + str(i), "description": "No task answer",
                            "start": {"dateTime": "2026-03-09T07:00:00-04:00", "timeZone": "America/New_York"},
                            "end": {"dateTime": "2026-03-09T08:00:00-04:00", "timeZone": "America/New_York"}}
                        ids.append(event_id(await call(client, i, "create_event", args)))
                        records.append(event(await call(client, i, "get_event", {"eventId": ids[i]})))
                        args.update({"summary": "DST elapsed-hour diagnostic", "start": {"dateTime": "2026-03-08T01:30:00-05:00", "timeZone": "America/New_York"},
                                     "end": {"dateTime": "2026-03-08T03:30:00-04:00", "timeZone": "America/New_York"}})
                        dst_ids.append(event_id(await call(client, i, "create_event", args)))
                        dst_records.append(event(await call(client, i, "get_event", {"eventId": dst_ids[i]})))
                    check(CHECKS[4], all(r["summary"] == "Component diagnostic slot " + str(i) and r["start"]["dateTime"] == "2026-03-09T11:00:00.000Z"
                          and r["start"]["timeZone"] == "America/New_York" for i, r in enumerate(records)))
                    check(CHECKS[5], all(r["start"]["dateTime"] == "2026-03-08T06:30:00.000Z" and r["end"]["dateTime"] == "2026-03-08T07:30:00.000Z"
                          for r in dst_records))
                    same_id = str(uuid.uuid4())
                    for i, cluster in enumerate(clusters):
                        query(cluster, "INSERT INTO gcal.events(id,summary,start_datetime,end_datetime) VALUES (%s,%s,%s,%s)",
                              (same_id, "same-id-slot-" + str(i), "2026-03-10T10:00:00Z", "2026-03-10T11:00:00Z"))
                    same = [event(await call(client, i, "get_event", {"eventId": same_id}))["summary"] for i, client in enumerate(clients)]
                    check(CHECKS[6], same == ["same-id-slot-0", "same-id-slot-1"])
                    absent = await call(clients[1], 1, "get_event", {"eventId": ids[0]})
                    check(CHECKS[7], absent.isError and "Event not found" in absent.content[0].text)
                    content(await call(clients[0], 0, "update_event", {"eventId": same_id, "summary": "only-first"}))
                    modified = [event(await call(client, i, "get_event", {"eventId": same_id}))["summary"] for i, client in enumerate(clients)]
                    check(CHECKS[8], modified == ["only-first", "same-id-slot-1"])
                    content(await call(clients[0], 0, "delete_event", {"eventId": same_id}))
                    deleted = await call(clients[0], 0, "get_event", {"eventId": same_id})
                    still = event(await call(clients[1], 1, "get_event", {"eventId": same_id}))
                    check(CHECKS[9], deleted.isError and still["summary"] == "same-id-slot-1")
                    listed = content(await call(clients[1], 1, "list_events", {"timeMin": "2026-03-08T06:30:00Z", "timeMax": "2026-03-08T07:30:00Z", "maxResults": 100}))
                    items = json.loads(listed.split("\n", 1)[1])
                    check(CHECKS[10], [item["id"] for item in items] == [dst_ids[1]])
                    counts = [require_schema(c) for c in clusters]
                    observations["before_invalid_counts"] = counts
                    bad = await call(clients[0], 0, "create_event", {"summary": 123})
                    observations["after_invalid_counts"] = [require_schema(c) for c in clusters]
                    check(CHECKS[11], bad.isError and counts == observations["after_invalid_counts"])
                    unknown = await call(clients[0], 0, "workagent_no_such_calendar_tool", {})
                    check(CHECKS[12], unknown.isError and "Unknown tool" in unknown.content[0].text)
                    (workspaces[0] / "Weekly_Meal_Plan.xlsx").write_bytes(b"Diagnostic reset sentinel, not an answer")
                    initialize(ROOT, build, clusters[0])
                    observations["after_reset_counts"] = [require_schema(c) for c in clusters]
                    check(CHECKS[13], observations["after_reset_counts"] == [0, counts[1]]
                          and not (workspaces[0] / "Weekly_Meal_Plan.xlsx").exists())
                    query(clusters[0], "DROP TABLE gcal.events")
                    blocked = False
                    try: initialize(ROOT, build, clusters[0])
                    except ValueError as exc:
                        blocked = True
                        observations["missing_schema_error"] = {"type": type(exc).__name__, "message": str(exc)}
                    check(CHECKS[14], blocked)
            asyncio.run(exercise())
            check(CHECKS[15], len(sessions) == 2 and all(s.get("closed") for s in sessions))
            for cluster in clusters:
                for path in cluster.profile.base.glob("calendar-preprocess-*.stdout.txt"): retain(path)
                for path in cluster.profile.base.glob("calendar-process-*.session.json"): retain(path)
                retain(cluster.profile.base / "cluster_owner.json")
        check(CHECKS[16], all(not c.started and not (c.profile.data / "postmaster.pid").exists() for c in clusters))
        after = shared_snapshot()
        check(CHECKS[17], before == after)
    finally:
        raw = (json.dumps(journal, ensure_ascii=False, indent=2) + "\n").encode()
        (output / "tool_journal.json").write_bytes(raw)
        result = {"schema_version": "original-calendar-isolation-diagnostic-v1", "at": datetime.now(timezone.utc).isoformat(),
            "passed": [c["name"] for c in checks] == list(CHECKS) and all(c["passed"] for c in checks), "checks": checks,
            "execution_kind": "original_mcp_component_diagnostic", "profiles": profiles, "sessions": sessions,
            "shared_before": before, "shared_after": after, "build_fingerprint": build["build_fingerprint"],
            "database_observations": observations,
            "journal_sha256": hashlib.sha256(raw).hexdigest(), "retained_sha256": retained,
            "source_sha256": {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in {*SOURCES, *build["source_sha256"]}},
            "runtime_task_activated": False, "nemo_second_task_enabled": False, "real_model_calls": 0, "training_updates": 0}
        save(output / "result.json", result)
    print(json.dumps({"passed": result["passed"], "checks": len(checks), "receipt": (output / "result.json").relative_to(ROOT).as_posix()}))
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
