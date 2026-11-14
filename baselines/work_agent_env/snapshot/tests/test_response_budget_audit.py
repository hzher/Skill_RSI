from copy import deepcopy
import json
import unittest
from uuid import UUID

from integrations.meal_plan_services import METHODS
from integrations.response_budget_audit import DESCRIPTION, LIST_ARGUMENTS, inspect_journal


class ResponseBudgetAuditTests(unittest.TestCase):
    def setUp(self):
        self.seed = {"resources_session_id": str(UUID(int=500)), "episode_id": {"rollout_id": "unit-response-budget", "attempt": 0},
            "task_id": {"taskset": "toolathlon_gym:unit", "task_id": "howtocook-meal-plan-gcal"}, "task_data": {}}
        bound = {k: self.seed[k] for k in ("resources_session_id", "episode_id")}
        def frame(route, body, response, status=200):
            return {"route": "/isolated/" + route, "status": status, "request": body, "response": response}
        def tool(identifier, name, arguments, result=None, status=200):
            return frame("tool", {**bound, "request_id": str(UUID(int=identifier)), "server": "google_calendar",
                "name": name, "arguments": arguments}, {"result": result} if status == 200 else {"detail": "ResourceError"}, status)
        self.events, creates = [], []
        for i in range(20):
            args = {"summary": "UTF-8 response diagnostic " + str(i), "description": DESCRIPTION}
            identifier = str(UUID(int=i + 1))
            creates.append(tool(i + 100, "create_event", args,
                {"content": [{"type": "text", "text": "Event created with ID: " + identifier + "\nTitle: unit"}]}))
            self.events.append({"id": identifier, **args})
        listing = tool(40, "list_events", dict(LIST_ARGUMENTS), {"content": [{"type": "text",
            "text": "Found 16 events:\n" + json.dumps(self.events[:16], ensure_ascii=False)}]})
        oversized = tool(41, "list_events", dict(LIST_ARGUMENTS), status=503)
        replay = {**deepcopy(oversized), "status": 409}
        fresh = tool(42, "create_event", {"summary": "must-not-execute"}, status=503)
        verify = frame("verify", {"episode_id": self.seed["episode_id"], "task_id": self.seed["task_id"],
            "verification_input": {"resources_session_id": self.seed["resources_session_id"]}},
            {"episode_id": self.seed["episode_id"], "task_id": self.seed["task_id"], "result": {"reward": None, "mask_sample": True}})
        self.journal = [frame("seed_session", self.seed, bound), frame("tools", bound,
            {"tools": [{"server": server, "name": name} for server, names in METHODS.items() for name in names]})]
        self.journal += creates[:16] + [listing, deepcopy(listing)] + creates[16:] + [oversized, replay, fresh, verify, frame("close_session", bound, bound)]

    def test_complete_unicode_result_and_no_replay_reconstruct_original_calls(self):
        facts = inspect_journal(self.journal, self.seed)
        self.assertGreater(facts["mcp_utf8_bytes"], 3 * 1024 * 1024)
        self.assertLess(facts["mcp_utf8_bytes"], 4 * 1024 * 1024)
        self.assertGreater(facts["mcp_ascii_bytes"], 4 * 1024 * 1024)
        self.assertEqual((len(facts["trace"]), len(facts["sql_after_twenty"])), (21, 20))
        self.assertEqual(sum(row[2] for row in facts["sql_after_twenty"]), 4200000)

    def test_truncation_and_changed_unicode_are_rejected_even_with_success_status(self):
        for changed in (self.events[:15], [{**e, "description": DESCRIPTION[:-1]} for e in self.events[:16]]):
            with self.subTest(count=len(changed)):
                journal = deepcopy(self.journal)
                result = journal[18]["response"]["result"]
                result["content"][0]["text"] = "Found " + str(len(changed)) + " events:\n" + json.dumps(changed, ensure_ascii=False)
                journal[19]["response"] = deepcopy(journal[18]["response"])
                with self.assertRaisesRegex(ValueError, "truncated or changed"):
                    inspect_journal(journal, self.seed)

    def test_cached_response_and_replay_request_must_match_exactly(self):
        for index in (19, 25):
            with self.subTest(index=index):
                journal = deepcopy(self.journal)
                journal[index]["request"]["request_id"] = str(UUID(int=999))
                with self.assertRaisesRegex(ValueError, "cache|replay"):
                    inspect_journal(journal, self.seed)

    def test_list_requires_the_fixed_original_time_window(self):
        for index in (18, 24):
            journal = deepcopy(self.journal)
            journal[index]["request"]["arguments"].pop("timeMin")
            with self.assertRaises(ValueError):
                inspect_journal(journal, self.seed)

    def test_unknown_cannot_be_zero_false_or_unmasked(self):
        for field, value in (("reward", 0), ("reward", False), ("mask_sample", False)):
            with self.subTest(field=field, value=value):
                journal = deepcopy(self.journal)
                journal[-2]["response"]["result"][field] = value
                with self.assertRaisesRegex(ValueError, "numeric task reward"):
                    inspect_journal(journal, self.seed)

    def test_resource_rebinding_or_missing_rejection_invalidates_raw_evidence(self):
        journal = deepcopy(self.journal)
        journal[2]["request"]["resources_session_id"] = str(UUID(int=999))
        with self.assertRaisesRegex(ValueError, "resource or episode"):
            inspect_journal(journal, self.seed)
        journal = deepcopy(self.journal)
        journal[24]["status"] = 200
        with self.assertRaisesRegex(ValueError, "HTTP sequence"):
            inspect_journal(journal, self.seed)


if __name__ == "__main__":
    unittest.main()
