"""Audit complete original MCP UTF-8 responses and fail-closed oversize handling."""
import hashlib
import json
import re
from uuid import UUID

from integrations.episode_cluster import EpisodeProfile, require_dependents_closed
from integrations.mainline_audit import read_json, source_binding, track_absence, track_input
from integrations.meal_plan_services import METHODS, MAX_ARGUMENT_BYTES, components
from integrations.resource_transport import MAX_RESPONSE_FRAME_BYTES, MAX_TOOL_RESPONSE_BYTES, wire_json
from integrations.runtime_identity import expected_identity
from integrations.task_routing import TaskRouter
from workenv.core import contained

CHECKS = ("original_meal_profile_and_empty_calendar", "sixteen_original_large_description_writes",
    "complete_chinese_response_above_old_frame_limit", "cached_large_result_is_byte_exact",
    "twenty_events_exceed_tool_response_budget", "oversized_result_is_unknown",
    "unknown_request_cannot_be_replayed", "unknown_service_blocks_new_business",
    "unknown_measurement_is_masked", "rejections_preserve_twenty_events",
    "closed_children_worker_and_postgres", "other_ledgers_and_shared_database_unchanged")
EXTRA_SOURCES = ("scripts/verify_nemo_response_budget.py", "integrations/response_budget_audit.py",
    "integrations/nemo_http.py", "scripts/verify_calendar_isolation.py", "workenv/process_identity.py")
MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
DESCRIPTION = ("\u83dc\U0001f35c") * 30000
SCHEMA = "nemo-original-response-budget-diagnostic-v1"
LIST_ARGUMENTS = {"timeMin": "2026-03-09T00:00:00Z", "timeMax": "2026-03-10T00:00:00Z", "maxResults": 100}


def created_id(result):
    text = result["content"][0]["text"]
    matched = re.match(r"Event created with ID: ([0-9a-f-]+)\n", text)
    if result.get("isError") or not matched or str(UUID(matched[1])) != matched[1]:
        raise ValueError("Original calendar creation acknowledgement differs")
    return matched[1]


def listed_events(result):
    if result.get("isError") or len(result["content"]) != 1:
        raise ValueError("Original calendar listing did not return complete text")
    header, text = result["content"][0]["text"].split("\n", 1)
    events = json.loads(text)
    if not isinstance(events, list) or header != "Found " + str(len(events)) + " events:":
        raise ValueError("Original calendar listing count differs")
    return events


def inspect_journal(journal, seed):
    """Reconstruct the bounded scenario from raw HTTP requests and replies."""
    if not isinstance(journal, list) or len(journal) != 29:
        raise ValueError("Response-budget HTTP inventory differs")
    expected = [("/isolated/seed_session", 200), ("/isolated/tools", 200)]
    expected += [("/isolated/tool", 200)] * 22
    expected += [("/isolated/tool", 503), ("/isolated/tool", 409), ("/isolated/tool", 503),
                 ("/isolated/verify", 200), ("/isolated/close_session", 200)]
    if [(e["route"], e["status"]) for e in journal] != expected or journal[0]["request"] != seed:
        raise ValueError("Response-budget HTTP sequence or seed differs")
    if seed["task_id"]["task_id"] != "howtocook-meal-plan-gcal" or seed["task_data"] != {}:
        raise ValueError("Response-budget task differs")
    for event in journal[1:]:
        request = event["request"]
        sid = request.get("verification_input", {}).get("resources_session_id") if event["route"].endswith("/verify") else request.get("resources_session_id")
        if sid != seed["resources_session_id"] or request.get("episode_id") != seed["episode_id"]:
            raise ValueError("Response-budget request changed resource or episode")
    schema = journal[1]["response"]
    if {(t["server"], t["name"]) for t in schema["tools"]} != {(s, n) for s, names in METHODS.items() for n in names}:
        raise ValueError("Response-budget original MCP methods differ")
    tools = journal[2:27]
    if any(e["request"]["server"] != "google_calendar" for e in tools):
        raise ValueError("Response-budget changed original service")
    creates = tools[:16] + tools[18:22]
    ids, mutations, trace = [], {}, []
    for index, event in enumerate(creates):
        request, response = event["request"], event["response"]
        args = request["arguments"]
        if (request["name"] != "create_event" or args.get("description") != DESCRIPTION
            or args.get("summary") != "UTF-8 response diagnostic " + str(index)
            or len(wire_json(args)) > MAX_ARGUMENT_BYTES):
            raise ValueError("Response-budget original mutation payload differs")
        identifier = created_id(response["result"])
        if identifier in mutations:
            raise ValueError("Response-budget mutation identity was reused")
        ids.append(identifier)
        mutations[identifier] = args
    first, repeated = tools[16:18]
    if (first["request"]["name"] != "list_events" or first["request"]["arguments"] != dict(LIST_ARGUMENTS)
        or repeated["request"] != first["request"] or repeated["response"] != first["response"]):
        raise ValueError("Large response cache differs from the original request or result")
    events = listed_events(first["response"]["result"])
    if (len(events) != 16 or {e["id"] for e in events} != set(ids[:16])
        or any(e.get("description") != mutations[e["id"]]["description"]
            or e.get("summary") != mutations[e["id"]]["summary"] for e in events)):
        raise ValueError("Large original response was truncated or changed")
    mcp_bytes = len(wire_json(first["response"]["result"]))
    http_bytes = len(wire_json(first["response"]))
    ascii_bytes = len(json.dumps(first["response"]["result"], ensure_ascii=True).encode("utf-8"))
    if not 3 * 1024 * 1024 < mcp_bytes <= MAX_TOOL_RESPONSE_BYTES or http_bytes > MAX_RESPONSE_FRAME_BYTES or ascii_bytes <= MAX_RESPONSE_FRAME_BYTES:
        raise ValueError("Large response does not demonstrate UTF-8 transport across the old limit")
    oversized, replay, fresh = tools[22:25]
    if (oversized["request"]["name"] != "list_events" or oversized["request"]["arguments"] != dict(LIST_ARGUMENTS)
        or oversized["request"] != replay["request"] or oversized["request"]["request_id"] == first["request"]["request_id"]
        or fresh["request"]["name"] != "create_event" or fresh["request"]["arguments"].get("summary") != "must-not-execute"):
        raise ValueError("Oversized response replay/fresh-business rejection differs")
    unique_requests = {e["request"]["request_id"] for e in tools}
    if len(unique_requests) != 23:
        raise ValueError("Response-budget operation identities differ")
    verify = journal[-2]
    measurement = verify["response"]["result"]
    if (verify["request"]["task_id"] != seed["task_id"] or verify["response"]["task_id"] != seed["task_id"]
        or verify["response"]["episode_id"] != seed["episode_id"] or measurement.get("reward") is not None
        or measurement.get("mask_sample") is not True):
        raise ValueError("Unknown response became a numeric task reward")
    seen = set()
    for event in tools:
        request = event["request"]
        if event["status"] != 200 or request["request_id"] in seen:
            continue
        seen.add(request["request_id"])
        trace.append({"type": "original_mcp", "server": request["server"], "name": request["name"],
                      "arguments": request["arguments"], "response": event["response"]["result"]})
    sql_rows = sorted([[identifier, mutations[identifier]["summary"], len(DESCRIPTION.encode("utf-8")),
                        hashlib.md5(DESCRIPTION.encode("utf-8")).hexdigest()] for identifier in ids])
    return {"mcp_utf8_bytes": mcp_bytes, "http_utf8_bytes": http_bytes, "mcp_ascii_bytes": ascii_bytes,
            "events": 20, "successful_original_calls": len(trace), "trace": trace,
            "sql_after_sixteen": [row for row in sql_rows if row[0] in set(ids[:16])], "sql_after_twenty": sql_rows}


def audit_response_budget(root, path):
    receipt = read_json(path, root)
    if (receipt.get("schema_version") != SCHEMA or receipt.get("passed") is not True
        or receipt.get("framework") != "nemo_gym" or receipt.get("cleanup_errors") != []
        or [c["name"] for c in receipt["checks"]] != list(CHECKS) or any(c["passed"] is not True for c in receipt["checks"])
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates", "training_admissions"))
        or any(receipt.get(k) is not False for k in ("training_eligible", "reference_solutions_read", "verified_task_bodies_read"))):
        raise ValueError("Response-budget diagnostic scope differs")
    expected = {**expected_identity(root), **{name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in EXTRA_SOURCES}}
    if receipt["source_sha256"] != expected:
        raise ValueError("Response-budget source inventory or bytes differ")
    source_binding(root, receipt["source_sha256"])
    retained = receipt["retained_sha256"]
    if not isinstance(retained, dict) or not 10 <= len(retained) <= 32:
        raise ValueError("Response-budget retained inventory differs")
    total = 0
    for name, digest in retained.items():
        member = contained(root, name)
        total += member.stat().st_size
        if member.stat().st_size > MAX_EVIDENCE_BYTES or total > 64 * 1024 * 1024:
            raise ValueError("Response-budget evidence exceeds its budget")
        with member.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        track_input(member, root, actual)
        if actual != digest:
            raise ValueError("Response-budget retained evidence changed")
    def value(name):
        if name not in retained:
            raise ValueError("Response-budget input lacks a retained binding")
        return read_json(contained(root, name), root, limit=MAX_EVIDENCE_BYTES)
    journal = value(receipt["journal_ref"])
    facts = inspect_journal(journal, receipt["seed"])
    observations = value(receipt["observations_ref"])
    raw = receipt["profile"]
    profile = EpisodeProfile(root, raw["resource_id"], raw["slot"])
    profile.validate_record(raw)
    def local(name):
        return value((profile.base / name).relative_to(root).as_posix())
    if local("cluster_owner.json") != raw or profile.resource_id != receipt["seed"]["resources_session_id"]:
        raise ValueError("Response-budget resource owner differs")
    route = TaskRouter(root, "isolated").authorize(receipt["seed"])
    if local("meal-task-session/task_profile.json") != route or journal[1]["response"]["task_profile"] != route:
        raise ValueError("Response-budget task profile differs")
    if local("meal-task-session/nemo_binding.json") != receipt["seed"]:
        raise ValueError("Response-budget retained NeMo binding differs")
    _, bindings, _ = components(root)
    for name in bindings:
        read_json(contained(root, name), root)
    if local("meal-task-session/initial_state.json")["component_receipt_sha256"] != bindings:
        raise ValueError("Response-budget original component identities differ")
    services = local("meal-task-session/services-closed.json")
    if (services["state"] != "closed" or services["outcome_unknown"] is not True or services["calls"] != 22
        or services["profile"] != raw or services["component_receipt_sha256"] != bindings):
        raise ValueError("Response-budget unknown outcome or original call count differs")
    if local("meal-task-session/trace.json") != facts["trace"]:
        raise ValueError("Response-budget original calls were changed or replayed")
    ledger = local("resources-session.json")["sessions"][profile.resource_id]
    if ledger["state"] != "closed" or ledger["binding"] != receipt["seed"]:
        raise ValueError("Response-budget local identity did not close")
    gateway = observations["gateway_closed_row"]
    if gateway["state"] != "closed" or gateway["binding"] != receipt["seed"]:
        raise ValueError("Response-budget gateway identity did not close")
    before, after = observations["worker_before"], observations["worker_after"]
    if (before.get("status") != "alive" or not before.get("creation_id") or type(before.get("pid")) is not int
        or type(after.get("pid")) is not int or after.get("pid") != before["pid"] or after.get("status") not in ("not_found", "exited")):
        raise ValueError("Response-budget worker exit identity differs")
    local("dependent_processes.json")
    require_dependents_closed(profile)
    track_absence(profile.data / "postmaster.pid", root)
    if (observations["initial_calendar_rows"] != [] or observations["rows_after_sixteen"] != facts["sql_after_sixteen"]
        or observations["rows_after_twenty"] != facts["sql_after_twenty"]
        or observations["rows_after_rejections"] != facts["sql_after_twenty"]
        or sum(row[2] for row in facts["sql_after_twenty"]) <= MAX_TOOL_RESPONSE_BYTES):
        raise ValueError("Response-budget rejected operations changed the calendar")
    ledgers_before, ledgers_after = observations["other_ledgers_before"], observations["other_ledgers_after"]
    if (ledgers_before != ledgers_after or set(ledgers_before) != {"resource-sessions.json", "synthetic-sessions.json"}
        or any(not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value) for value in ledgers_before.values())):
        raise ValueError("Response-budget changed another ledger")
    if (not isinstance(receipt["shared_before"], dict) or len(receipt["shared_before"]) != 6
        or receipt["shared_before"] != receipt["shared_after"]):
        raise ValueError("Response-budget changed the shared database")
    return {"checks": len(CHECKS), "mcp_utf8_bytes": facts["mcp_utf8_bytes"], "http_utf8_bytes": facts["http_utf8_bytes"],
        "mcp_ascii_bytes": facts["mcp_ascii_bytes"], "complete_returned_events": 16, "retained_calendar_events": 20,
        "unknown_requests_replayed": 0, "training_admissions": 0}
