import json
import tempfile
import unittest
from pathlib import Path

from clm.context import Context, editable_exchanges, encode, write_json


def exchange(tool_id, text="old context " * 100):
    return [
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "private", "signature": "intact"},
            {"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": "test"}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]},
    ]


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.ctx = Context(self.path)
        self.user = {"role": "user", "content": "protected original task"}
        self.history = [self.user, *exchange("old"), *exchange("recent")]
        self.key = next(iter(editable_exchanges(self.history)))
        self.ctx.apply(self.history)

    def submit(self, drop, note="", revision=1):
        write_json(self.path / "edit.json", {"drop": drop, "note": note, "revision": revision})

    def test_edit_applies_to_next_and_later_requests(self):
        self.submit([self.key], "the value is in archive.txt")
        result, event = self.ctx.apply(self.history + exchange("edit"))
        self.assertEqual(event["edit"], "accepted")
        self.assertEqual(event["removed_this_request"], 2)
        self.assertNotIn("old", [block.get("id") for msg in result[1:] for block in msg["content"]])
        self.assertTrue(result[0]["content"][1]["text"].endswith("archive.txt"))
        self.assertEqual(result[1:], self.history[3:] + exchange("edit"))
        self.assertEqual(self.ctx.apply(self.history + exchange("edit"))[0], result)

    def test_unknown_and_stale_edits_preserve_history(self):
        self.submit(["not-editable"])
        self.assertEqual(self.ctx.apply(self.history), (self.history, {
            "revision": 2, "edit": "protected_or_unknown_exchange", "dropped_ids": [],
            "removed_this_request": 0, "editable_count": 1,
            "before_message_bytes": len(encode(self.history)),
            "after_message_bytes": len(encode(self.history)),
        }))
        self.submit([self.key])
        self.assertEqual(self.ctx.apply(self.history)[1]["edit"], "stale_revision")

    def test_growth_rejected_and_count_tokens_does_not_consume_edit(self):
        self.submit([self.key], "a" * 4096)
        self.assertEqual(self.ctx.apply(self.history, accept_edit=False)[0], self.history)
        self.assertEqual(self.ctx.apply(self.history)[1]["edit"], "edit_must_shrink")

    def test_multi_tool_mismatch_and_user_text_protected(self):
        broken = exchange("old")
        broken[1]["content"][0]["tool_use_id"] = "different-id"
        self.assertFalse(editable_exchanges([self.user, *broken, *exchange("recent")]))
        with_user_text = exchange("old")
        with_user_text[1]["content"].append({"type": "text", "text": "new user instruction"})
        self.assertFalse(editable_exchanges([self.user, *with_user_text, *exchange("recent")]))

    def test_cache_marker_does_not_change_editable_id(self):
        self.history[2]["content"][0]["cache_control"] = {"type": "ephemeral"}
        self.assertEqual(list(editable_exchanges(self.history)), [self.key])

    def test_new_anchor_discards_previous_overlay(self):
        self.submit([self.key])
        self.ctx.apply(self.history)
        different = [{"role": "user", "content": "new session"}, *self.history[1:]]
        self.assertEqual(self.ctx.apply(different)[0], different)
        self.assertEqual(json.loads((self.path / "LIVE_CTX_MAIN.json").read_text())["note"], "")


if __name__ == "__main__":
    unittest.main()
