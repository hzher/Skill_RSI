"""Tampered component transcripts cannot retain calendar isolation claims."""
from copy import deepcopy
import json
import unittest

from integrations.calendar_audit import validate_journal


def transcript():
    profiles = [{"resource_id": "fixture-slot-0"}, {"resource_id": "fixture-slot-1"}]
    rows = []
    def add(slot, name, args, text, error=False):
        rows.append({"episode": profiles[slot]["resource_id"], "slot": slot, "name": name, "arguments": args,
                     "response": {"isError": error, "content": [{"type": "text", "text": text}]}})
    def detail(sid, summary, start, end="2026-03-08T07:30:00.000Z"):
        return json.dumps({"id": sid, "summary": summary, "start": {"dateTime": start, "timeZone": "America/New_York"}, "end": {"dateTime": end}})
    for slot in (0, 1):
        for kind, start in (("normal", "2026-03-09T11:00:00.000Z"), ("dst", "2026-03-08T06:30:00.000Z")):
            sid = ("a" if kind == "normal" else "b") + str(slot)
            add(slot, "create_event", {}, "Event created with ID: " + sid + "\n")
            add(slot, "get_event", {"eventId": sid}, detail(sid, "fixture", start))
    for slot in (0, 1): add(slot, "get_event", {"eventId": "common"}, detail("common", "same-id-slot-" + str(slot), "unused"))
    add(1, "get_event", {"eventId": "a0"}, "Error: Event not found", True)
    add(0, "update_event", {"eventId": "common", "summary": "only-first"}, "Event updated: common")
    add(0, "get_event", {"eventId": "common"}, detail("common", "only-first", "unused"))
    add(1, "get_event", {"eventId": "common"}, detail("common", "same-id-slot-1", "unused"))
    add(0, "delete_event", {"eventId": "common"}, "Event deleted: common")
    add(0, "get_event", {"eventId": "common"}, "Error: Event not found", True)
    add(1, "get_event", {"eventId": "common"}, detail("common", "same-id-slot-1", "unused"))
    add(1, "list_events", {"timeMin": "2026-03-08T06:30:00Z", "timeMax": "2026-03-08T07:30:00Z", "maxResults": 100}, 'Found 1 events:\n[{"id":"b1"}]')
    add(0, "create_event", {"summary": 123}, "Error: Invalid arguments", True)
    add(0, "workagent_no_such_calendar_tool", {}, "Error: Unknown tool", True)
    return profiles, rows


class CalendarAuditTests(unittest.TestCase):
    def test_complete_component_fixture_has_no_model_or_task_execution_claim(self):
        profiles, rows = transcript()
        self.assertEqual(validate_journal(profiles, rows)["calls"], 20)

    def test_episode_rebind_or_missing_call_is_rejected(self):
        profiles, rows = transcript()
        rows[9]["episode"] = profiles[0]["resource_id"]
        with self.assertRaises(ValueError): validate_journal(profiles, rows)
        with self.assertRaises(ValueError): validate_journal(profiles, rows[:-1])

    def test_invalid_argument_success_or_hidden_tool_error_is_rejected(self):
        for index in (0, 18):
            profiles, rows = transcript()
            rows[index]["response"]["isError"] = not rows[index]["response"]["isError"]
            with self.assertRaises(ValueError): validate_journal(profiles, rows)

    def test_wrong_utc_offset_and_dst_duration_are_rejected(self):
        for which in ("start", "end"):
            profiles, rows = transcript()
            detail = json.loads(rows[3]["response"]["content"][0]["text"])
            detail[which]["dateTime"] = "2026-03-08T08:30:00.000Z"
            rows[3]["response"]["content"][0]["text"] = json.dumps(detail)
            with self.assertRaises(ValueError): validate_journal(profiles, rows)

    def test_update_leaking_to_second_episode_is_rejected(self):
        profiles, rows = transcript()
        detail = json.loads(rows[13]["response"]["content"][0]["text"])
        detail["summary"] = "only-first"
        rows[13]["response"]["content"][0]["text"] = json.dumps(detail)
        with self.assertRaises(ValueError): validate_journal(profiles, rows)

    def test_wrong_range_or_cross_episode_identifier_is_rejected(self):
        profiles, rows = transcript()
        altered = deepcopy(rows)
        altered[17]["arguments"]["timeMax"] = "2026-03-08T07:29:59Z"
        with self.assertRaises(ValueError): validate_journal(profiles, altered)
        rows[10]["arguments"]["eventId"] = "a1"
        with self.assertRaises(ValueError): validate_journal(profiles, rows)
