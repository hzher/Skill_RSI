"""Retained generator edits must not silently overwrite conflicting candidate branches."""
import unittest
from scripts.recover_skill_responses import merge_edits


class ResponseMergeTests(unittest.TestCase):
    def test_independent_inserts_combine(self):
        merged, conflicts = merge_edits({"A": "one"}, {}, {"B": "two"})
        self.assertEqual(merged, {"A": "one", "B": "two"})
        self.assertEqual(conflicts, [])

    def test_conflicting_update_and_delete_preserve_target(self):
        merged, conflicts = merge_edits({"A": "changed", "B": "changed"},
            {"A": "old", "B": "old"}, {"A": "new"})
        self.assertEqual(merged, {"A": "changed", "B": "changed"})
        self.assertEqual(conflicts, ["A", "B"])

    def test_matching_parent_can_update_and_delete(self):
        merged, conflicts = merge_edits({"A": "old", "B": "old"},
            {"A": "old", "B": "old"}, {"A": "new"})
        self.assertEqual(merged, {"A": "new"})
        self.assertEqual(conflicts, [])
