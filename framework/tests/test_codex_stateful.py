import json
import sys
import tempfile
import unittest
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from skillrl import codex_stateful as stateful
from skillrl.storage import PendingStage


@dataclass
class Outcome:
    instance_id: str
    instance_index: int
    success: bool = True
    reward: float = 1.0


@dataclass
class Observation:
    content: str


@dataclass
class Verdict:
    instance_outcomes: list


class Action:
    @classmethod
    def model_json_schema(cls):
        return {"type": "object"}

    @classmethod
    def model_validate(cls, value):
        if value != {"answer": "done"}:
            raise ValueError("Expected an answer")
        return cls()

    def model_dump(self, mode):
        return {"answer": "done"}


class NativeFamily:
    """The first action resolves its own instance and an intervening automatic instance."""
    def __init__(self, family, instances):
        self.name = family
        self.instances = range(instances)
        self.outcomes = []
        self.steps = self.resets = self.evaluations = self.closures = 0

    def prepare_run(self, index):
        if index != 0:
            raise ValueError("Unexpected run")

    def query(self, i):
        return SimpleNamespace(prompt="Complete this task", response_schema=Action, instance_index=i,
                               instance_id=f"private-policy-{self.name}-{i}", metadata={})

    def reset(self):
        self.resets += 1
        return self.query(0)

    def step(self, response):
        self.steps += 1
        i = len(self.outcomes)
        latest = Outcome(self.query(i).instance_id, i)
        self.outcomes.append(latest)
        if i == 0 and len(self.instances) > 1:
            self.outcomes.append(Outcome(self.query(1).instance_id, 1))
        done = len(self.outcomes) == len(self.instances)
        return SimpleNamespace(done=done, instance_outcome=latest, observation=Observation("Accepted"),
                               next_query=None if done else self.query(len(self.outcomes)))

    def get_instance_outcomes(self):
        return self.outcomes

    def evaluate(self):
        self.evaluations += 1
        return Verdict(self.outcomes)

    def _cleanup_container(self):
        self.closures += 1


def tasks(family, count):
    return [{"family": family, "canonical_index": i, "id": f"{family}-{i:03d}"} for i in range(count)]


class StatefulTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {"backend": "codex", "model": "synthetic", "repository": str(self.root)}
        self.families = []
        self.invocations = []
        self.write_mcp = True

        def registry(family):
            def create(schedule):
                self.assertEqual(schedule, "default")
                task = NativeFamily(family, 3)
                self.families.append(task)
                return task
            return create
        self.enterContext(patch.dict(sys.modules, {
            "src.registry": SimpleNamespace(get_task_class=registry),
            "src.interface": SimpleNamespace(Response=SimpleNamespace),
        }))
        self.enterContext(patch.object(sys, "path", list(sys.path)))
        self.transport = self.enterContext(patch.object(stateful.actor, "run_actor", side_effect=self.run_actor))

    def run_actor(self, config, case, episode, owner, prompt, *, family_workspace, resume_thread_id):
        self.assertNotIn("private-policy-", prompt)
        broker = stateful.actor.JournalBroker(episode, case / "tools", owner=owner, journal=case / "broker.jsonl")
        def call(number, tool, arguments):
            operation = "test-" + str(number)
            result = broker.call(operation, tool, arguments)
            if self.write_mcp:
                event = {"operation_id": operation, "session": "test", "result": result,
                         "request": {"id": number, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}}
                with (case / "mcp.jsonl").open("a") as stream:
                    stream.write(json.dumps(event) + "\n")
            return result
        status = call(1, "status", {})
        invalid = call(2, "act", {"action": {"invalid": True}})
        self.assertTrue(invalid["isError"])
        result = call(3, "act", {"action": {"answer": "done"}})
        self.assertFalse(result["isError"])
        self.assertEqual(result, call(3, "act", {"action": {"answer": "done"}}))
        for reply in (status, invalid, result):
            self.assertNotIn("private-policy-", str(reply))
        reply = json.loads(result["content"][0]["text"])
        self.assertEqual(reply["current"]["query"], None)
        self.assertTrue(reply["done"])
        thread = resume_thread_id or str(uuid.uuid4())
        self.invocations.append((episode.family.name, episode.index, resume_thread_id, thread))
        return {"complete": True, "usage": {"tokens": 10 if not resume_thread_id else 28}}, [thread], None

    def test_family_threads_automatic_results_and_safe_rerun(self):
        plan = tasks("alpha", 3) + tasks("beta", 3)
        outcome = stateful.execute_sequence(self.config, plan, self.root)
        self.assertEqual(outcome["completed_tasks"], 6)
        self.assertEqual([pair[:2] for pair in self.invocations],
                         [("alpha", 0), ("alpha", 2), ("beta", 0), ("beta", 2)])
        first, last, other_first, other_last = self.invocations
        self.assertIsNone(first[2])
        self.assertIsNone(other_first[2])
        self.assertEqual(first[3], last[2])
        self.assertEqual(other_first[3], other_last[2])
        self.assertNotEqual(first[3], other_first[3])
        for family in self.families:
            self.assertEqual((family.resets, family.steps, family.evaluations, family.closures), (1, 2, 1, 1))
        case = self.root / "cases"
        rows = [json.loads((p / "result.json").read_text()) for p in sorted(case.iterdir())]
        self.assertEqual([r["usage_delta"] for r in rows[:3]], [{"tokens": 10}, None, {"tokens": 18}])
        self.assertEqual([r["task_actions"] for r in rows[:3]], [1, 0, 1])
        self.assertEqual(rows[2]["continuity"]["previous_attempt_id"], rows[1]["identity"]["attempt_id"])
        self.assertTrue(all(r["score"] == 1 and r["complete"] for r in rows))
        self.assertEqual([r["mcp_calls"] for r in rows[:3]], [4, 0, 4])
        self.assertEqual(stateful.execute_sequence(self.config, plan, self.root), outcome)
        self.assertEqual(len(self.families), 2)
        self.assertEqual(self.transport.call_count, 4)
        path = case / "001-alpha-000" / "result.json"
        path.write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            stateful.execute_sequence(self.config, plan, self.root)

    def test_failed_turn_is_closed_and_never_replayed(self):
        plan = tasks("alpha", 3)
        def lost_process(*args, **kwargs):
            self.run_actor(*args, **kwargs)
            raise RuntimeError("Lost actor after dispatch")
        self.transport.side_effect = lost_process
        with self.assertRaisesRegex(RuntimeError, "Lost actor"):
            stateful.execute_sequence(self.config, plan, self.root)
        case = self.root / "cases/001-alpha-000"
        record = json.loads((case / "result.json").read_text())
        self.assertFalse(record["execution_complete"])
        self.assertEqual(record["score"], 1)
        self.assertEqual(record["task_actions"], 1)
        self.assertTrue(json.loads((self.root / "families/alpha/close-receipt.json").read_text())["closed"])
        with self.assertRaisesRegex(PendingStage, "between sealed families"):
            stateful.execute_sequence(self.config, plan, self.root)
        self.assertEqual(self.families[0].steps, 1)
        self.assertEqual(self.transport.call_count, 1)

    def test_missing_mcp_delivery_preserves_native_verdict_without_certifying_trajectory(self):
        self.write_mcp = False
        with self.assertRaisesRegex(RuntimeError, "requires reconciliation"):
            stateful.execute_sequence(self.config, tasks("alpha", 3), self.root)
        record = json.loads((self.root / "cases/001-alpha-000/result.json").read_text())
        self.assertFalse(record["complete"])
        self.assertFalse(record["execution_complete"])
        self.assertFalse(record["journal_complete"])
        self.assertEqual((record["mcp_calls"], record["task_actions"], record["score"]), (0, 1, 1))

    def test_changed_mcp_payload_or_repeated_receipt_is_rejected(self):
        stateful.execute_sequence(self.config, tasks("alpha", 1), self.root)
        case = self.root / "cases/001-alpha-000"
        journal = case / "mcp.jsonl"
        original = journal.read_text()
        for field in ("result", "arguments", "repeat"):
            events = list(map(json.loads, original.splitlines()))
            if field == "result":
                events[2]["result"]["content"][0]["text"] = "changed return"
            elif field == "arguments":
                events[2]["request"]["params"]["arguments"] = {"action": {"different": True}}
            else:
                events.pop()
            journal.write_text("".join(json.dumps(v) + "\n" for v in events))
            with self.subTest(field=field):
                self.assertFalse(stateful.actor.check_journals(case, 1)["journal_complete"])

    def test_reject_gapped_repeated_and_interleaved_schedules_before_dispatch(self):
        for plan in ([], tasks("alpha", 2)[1:], tasks("alpha", 1) * 2,
                     [tasks("alpha", 2)[0], tasks("beta", 1)[0], tasks("alpha", 2)[1]]):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                stateful.execute_sequence(self.config, plan, self.root)
        self.assertEqual(self.transport.call_count, 0)
        self.assertFalse((self.root / "collection.json").exists())

    def test_invalid_cumulative_usage_is_unknown(self):
        self.assertIsNone(stateful.usage_delta({"tokens": 2}, {"tokens": 3}))
        self.assertIsNone(stateful.usage_delta({"input": 2}, {"tokens": 1}))
        self.assertIsNone(stateful.usage_delta({"tokens": 2}, None))
        self.assertIsNone(stateful.usage_delta({"tokens": 2}, {"tokens": "unknown"}))
