"""Raw protocol/journal checks reject weakened or rebound service evidence."""
from copy import deepcopy
import json
import unittest

from integrations.meal_services_audit import validate_journal


class MealServicesAuditTests(unittest.TestCase):
    def setUp(self):
        self.recipes = [{"id": "unit-recipe", "name": "Unit recipe", "description": "Unit description"}]
        def entry(server, name, args, text):
            return {"server": server, "name": name, "arguments": args, "response": {"isError": False, "content": [{"text": text}]}}
        self.journal = [entry("howtocook", "mcp_howtocook_getAllRecipes", {}, json.dumps([{k: self.recipes[0][k] for k in ("name", "description")}]))]
        self.journal.append(entry("howtocook", "mcp_howtocook_getRecipeById", {"query": "unit-recipe"}, json.dumps(self.recipes[0])))
        self.journal.append({"server": "howtocook", "name": "mcp_howtocook_getRecipeById", "arguments": {"query": 123}, "response": {"protocol_error": {"code": -32602}}})
        for i in range(21):
            identifier = f'00000000-0000-4000-8000-{i:012d}'
            self.journal.append(entry("google_calendar", "create_event", {"summary": "Cook: Unit recipe"}, f'Event created with ID: {identifier}\nfixture'))
        for summary in ("Cook: workagent-diagnostic-wrong-recipe", "Cook: Unit recipe"):
            self.journal.append(entry("google_calendar", "update_event", {"eventId": "00000000-0000-4000-8000-000000000000", "summary": summary}, "fixture"))

    def test_original_projection_protocol_error_and_calendar_identities(self):
        self.assertEqual(len(validate_journal(self.journal, self.recipes)), 21)

    def test_full_recipe_data_cannot_replace_the_original_listing_projection(self):
        self.journal[0]["response"]["content"][0]["text"] = json.dumps(self.recipes)
        with self.assertRaises(ValueError): validate_journal(self.journal, self.recipes)

    def test_missing_protocol_failure_or_wrong_server_is_rejected(self):
        changed = deepcopy(self.journal)
        changed[2]["response"]["protocol_error"]["code"] = -32603
        with self.assertRaises(ValueError): validate_journal(changed, self.recipes)
        self.journal[3]["server"] = "other_calendar"
        with self.assertRaises(ValueError): validate_journal(self.journal, self.recipes)

    def test_duplicate_or_rebound_mutation_ids_are_rejected(self):
        changed = deepcopy(self.journal)
        changed[4]["response"] = changed[3]["response"]
        with self.assertRaises(ValueError): validate_journal(changed, self.recipes)
        self.journal[24]["arguments"]["eventId"] = "different-id"
        with self.assertRaises(ValueError): validate_journal(self.journal, self.recipes)

    def test_shortened_or_failed_calendar_calls_are_rejected(self):
        with self.assertRaises(ValueError): validate_journal(self.journal[:-1], self.recipes)
        self.journal[5]["response"]["isError"] = True
        with self.assertRaises(ValueError): validate_journal(self.journal, self.recipes)
