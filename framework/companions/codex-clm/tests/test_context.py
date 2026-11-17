import json
import tempfile
import unittest
from pathlib import Path

from clm.context import Context, candidates, write_json


def message(role, text):
    return {"type": "message", "role": role, "content": text}


def exchange(call_id, custom=False):
    kind = "custom_tool_call" if custom else "function_call"
    return [
        {"type": "reasoning", "id": "rs_" + call_id, "encrypted_content": "opaque"},
        {"type": kind, "call_id": call_id, "name": "exec_command", "arguments": "{}"},
        {"type": kind + "_output", "call_id": call_id, "output": "old tool output " * 120},
    ]


class ContextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.context = Context(self.root)
        self.prefix = [message("developer", "instructions"), message("user", "task")]
        self.old = exchange("old")
        self.recent = exchange("recent", custom=True)
        self.input = self.prefix + self.old + self.recent
        self.old_id, = candidates(self.input, 1)
        initial, event = self.context.apply(self.input)
        self.assertEqual(initial, self.input)
        self.assertEqual(event["editable_count"], 1)

    def edit(self, ids, note="", revision=1):
        write_json(self.root / "edit.json", {"revision": revision, "drop": ids, "note": note})

    def test_complete_group_removed_and_reasoning_preserved_for_retained_group(self):
        self.edit([self.old_id], "Value saved in archive.txt")
        next_input = self.input + exchange("new")
        result, event = self.context.apply(next_input)
        self.assertEqual(event["edit"], "accepted")
        self.assertEqual(event["removed_items"], 3)
        self.assertLess(event["after_input_bytes"], event["before_input_bytes"])
        self.assertEqual(result[0], self.prefix[0])
        self.assertEqual(result[1]["content"][0]["text"], "task")
        self.assertTrue(result[1]["content"][1]["text"].endswith("archive.txt"))
        self.assertEqual(result[2:], self.recent + exchange("new"))
        self.assertEqual(self.context.apply(next_input)[0], result)

    def test_unmatched_duplicate_and_unknown_boundary_protect_tool_batch(self):
        for old in (
            self.old[:-1],
            self.old + [self.old[-1]],
            self.old[:2] + [{"type": "unknown_item"}] + self.old[2:],
        ):
            self.assertFalse(candidates(self.prefix + old + self.recent, 1))

    def test_parallel_calls_remove_only_complete_batch(self):
        a, b = exchange("a"), exchange("b", custom=True)
        batch = [a[0], a[1], b[1], b[2], a[2]]
        groups = candidates(self.prefix + batch + self.recent, 1)
        self.assertEqual(list(groups.values()), [(2, 7)])
        self.assertFalse(candidates(self.prefix + batch[:-1] + self.recent, 1))

    def test_unknown_or_stale_edit_keeps_old_view(self):
        self.edit(["a" * 24])
        result, event = self.context.apply(self.input)
        self.assertEqual(result, self.input)
        self.assertEqual(event["edit"], "protected_or_unknown_exchange")
        self.edit([self.old_id])
        self.assertEqual(self.context.apply(self.input)[1]["edit"], "stale_revision")

    def test_reject_growth_and_hide_opaque_reasoning_from_snapshot(self):
        self.edit([self.old_id], "z" * 6000)
        result, event = self.context.apply(self.input)
        self.assertEqual(result, self.input)
        self.assertEqual(event["edit"], "edit_must_shrink")
        self.assertNotIn("opaque", (self.root / "LIVE_CTX_MAIN.json").read_text())

    def test_new_anchor_discards_previous_overlay_and_protects_user(self):
        self.edit([self.old_id], "note")
        self.context.apply(self.input)
        changed = [self.prefix[0], message("user", "other"), *self.input[2:]]
        self.assertEqual(self.context.apply(changed)[0], changed)
        self.assertEqual(json.loads((self.root / "LIVE_CTX_MAIN.json").read_text())["note"], "")


if __name__ == "__main__":
    unittest.main()
