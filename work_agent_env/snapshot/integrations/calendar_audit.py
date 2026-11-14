"""Recheck original calendar component journals and retained episode identities."""
import hashlib
import json
import re
from pathlib import Path

from integrations.calendar_component import verify_build, pinned_sources
from integrations.episode_cluster import EpisodeProfile
from integrations.mainline_audit import read_json, source_binding, track_input, track_absence
from workenv.core import contained

CHECKS = ("owned_empty_calendar_schema_dump", "separate_owned_episode_profiles", "original_preprocess_clears_each_episode",
          "original_calendar_tool_schemas", "original_create_and_get_per_episode", "original_dst_elapsed_hour",
          "same_id_has_independent_values", "cross_episode_id_is_absent", "original_update_is_isolated",
          "original_delete_is_isolated", "inclusive_original_range_boundaries", "invalid_arguments_do_not_mutate",
          "unknown_tool_is_explicit", "original_reset_is_isolated_and_removes_workbook",
          "missing_schema_blocks_initialization", "both_original_calendar_children_closed",
          "both_owned_postmasters_stopped", "shared_original_resources_unchanged")
TOOLS = {"create_event", "get_event", "update_event", "delete_event", "list_events"}


def validate_journal(profiles, events):
    expected = ["create_event", "get_event", "create_event", "get_event"] * 2 + ["get_event", "get_event", "get_event",
        "update_event", "get_event", "get_event", "delete_event", "get_event", "get_event", "list_events", "create_event", "workagent_no_such_calendar_tool"]
    slots = [0] * 4 + [1] * 4 + [0, 1, 1, 0, 0, 1, 0, 0, 1, 1, 0, 0]
    if len(events) != 20 or [e["name"] for e in events] != expected: raise ValueError("Original calendar call sequence differs")
    text = []
    for i, (entry, slot) in enumerate(zip(events, slots)):
        if type(entry["slot"]) is not int or entry["slot"] != slot or entry["episode"] != profiles[slot]["resource_id"]:
            raise ValueError("Original calendar call rebound to another episode")
        response = entry["response"]
        if bool(response.get("isError")) != (i in (10, 15, 18, 19)):
            raise ValueError("Original calendar success/error classification differs")
        text.append(response["content"][0]["text"])
    ids, dst = [], []
    for start in (0, 4):
        for offset, utc in ((0, "2026-03-09T11:00:00.000Z"), (2, "2026-03-08T06:30:00.000Z")):
            created = re.match(r"Event created with ID: ([0-9a-f-]+)\n", text[start + offset])
            detail = json.loads(text[start + offset + 1])
            if (not created or detail["id"] != created[1] or events[start + offset + 1]["arguments"]["eventId"] != created[1]
                or detail["start"]["dateTime"] != utc or detail["start"]["timeZone"] != "America/New_York"):
                raise ValueError("Original calendar create/get/timezone identity differs")
            if offset == 0: ids.append(created[1])
            else:
                dst.append(created[1])
                if detail["end"]["dateTime"] != "2026-03-08T07:30:00.000Z": raise ValueError("DST elapsed duration differs")
    if ids[0] == ids[1] or events[10]["arguments"]["eventId"] != ids[0] or "Event not found" not in text[10]:
        raise ValueError("Cross-episode original event isolation differs")
    common = events[8]["arguments"]["eventId"]
    if any(events[i]["arguments"].get("eventId") != common for i in (9, 11, 12, 13, 14, 15, 16)):
        raise ValueError("Shared fixture identity changed")
    if [json.loads(text[i])["summary"] for i in (8, 9, 12, 13, 16)] != ["same-id-slot-0", "same-id-slot-1", "only-first", "same-id-slot-1", "same-id-slot-1"]:
        raise ValueError("Original calendar update/delete crossed episodes")
    listed = json.loads(text[17].split("\n", 1)[1])
    if (events[17]["arguments"] != {"timeMin": "2026-03-08T06:30:00Z", "timeMax": "2026-03-08T07:30:00Z", "maxResults": 100}
        or [item["id"] for item in listed] != [dst[1]] or events[18]["arguments"] != {"summary": 123}
        or "Event not found" not in text[15] or "Unknown tool" not in text[19]):
        raise ValueError("Original calendar range/error behavior differs")
    return {"calls": 20, "tools": sorted(TOOLS), "episodes": 2}


def audit_calendar(root, receipt_path):
    root, receipt_path = Path(root).resolve(), Path(receipt_path)
    receipt = read_json(receipt_path, root)
    source_binding(root, receipt["source_sha256"])
    if (receipt.get("schema_version") != "original-calendar-isolation-diagnostic-v1" or receipt.get("passed") is not True
        or receipt.get("execution_kind") != "original_mcp_component_diagnostic"
        or [r["name"] for r in receipt["checks"]] != list(CHECKS) or any(r["passed"] is not True for r in receipt["checks"])
        or any(receipt.get(k) is not False for k in ("runtime_task_activated", "nemo_second_task_enabled"))
        or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates"))):
        raise ValueError("Incomplete or promoted original calendar diagnostic")
    build = read_json(root / "evidence/mainline/calendar_build.json", root)
    source_binding(root, build["source_sha256"])
    base = verify_build(root, build)
    track_input(base / "build_owner.json", root)
    commit, originals, objects = pinned_sources(root)
    if build["source_commit"] != commit or build["source_objects"] != objects or receipt["build_fingerprint"] != build["build_fingerprint"]:
        raise ValueError("Original calendar source/build identity differs")
    for name, raw in originals.items():
        if (base / name).read_bytes() != raw: raise ValueError("Original calendar source was modified")
    for name, checksum in build["retained_sha256"].items(): track_input(contained(base, name), root, checksum)
    profiles = receipt["profiles"]
    if len(profiles) != 2 or len({p["resource_id"] for p in profiles}) != 2: raise ValueError("Missing independent calendar resources")
    for i, profile in enumerate(profiles):
        expected = EpisodeProfile(root, profile["resource_id"], i)
        expected.validate_record(profile)
        if read_json(expected.base / "cluster_owner.json", root) != profile: raise ValueError("Calendar cluster owner changed")
        track_absence(expected.data / "postmaster.pid", root)
    retained = receipt["retained_sha256"]
    if not 9 <= len(retained) <= 24: raise ValueError("Calendar retained artifact inventory differs")
    for name, checksum in retained.items():
        path = contained(root, name)
        if not name.startswith((".runtime/native-episodes/", receipt_path.parent.relative_to(root).as_posix() + "/")):
            raise ValueError("Foreign retained calendar artifact")
        if path.stat().st_size > 256 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError("Retained calendar evidence changed")
        track_input(path, root, checksum)
    journal_path = receipt_path.parent / "tool_journal.json"
    events = read_json(journal_path, root)
    if hashlib.sha256(journal_path.read_bytes()).hexdigest() != receipt["journal_sha256"]: raise ValueError("Calendar journal changed")
    summary = validate_journal(profiles, events)
    schemas = read_json(receipt_path.parent / "tool_schemas.json", root)
    if len(schemas) != 2 or any({t["name"] for t in s["tools"]} != TOOLS for s in schemas): raise ValueError("Original calendar schemas differ")
    shared = receipt["shared_before"]
    if shared != receipt["shared_after"] or set(shared) != {"wc.orders", "gsheet.spreadsheets", "gsheet.sheets", "gsheet.cells", "gsheet.permissions", "gcal.events"}:
        raise ValueError("Shared original resources changed")
    observed = receipt["database_observations"]
    if (observed["initialized_counts"] != [0, 0] or observed["before_invalid_counts"] != [2, 3]
        or observed["after_invalid_counts"] != [2, 3] or observed["after_reset_counts"] != [0, 3]
        or observed["missing_schema_error"]["type"] != "ValueError"):
        raise ValueError("Original calendar reset/invalid-input state differs")
    sessions = receipt["sessions"]
    if len(sessions) != 2: raise ValueError("Missing original calendar child closure evidence")
    for i, session in enumerate(sessions):
        before, after = session["before"], session["after"]
        closed = after["status"] in ("not_found", "exited") or bool(after.get("creation_id") and after["creation_id"] != before["creation_id"])
        if (session["profile"] != profiles[i] or session["build_fingerprint"] != build["build_fingerprint"] or session.get("closed") is not True
            or before["status"] != "alive" or not before.get("creation_id") or after["pid"] != before["pid"] or not closed):
            raise ValueError("Original calendar child identity or closure differs")
    return {**summary, "checks": len(CHECKS), "typecheck_completed": True, "nemo_second_task_enabled": False}
