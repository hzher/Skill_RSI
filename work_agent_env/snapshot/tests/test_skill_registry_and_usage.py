from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from integrations.model_usage import ModelUsageLedger
from training.skill_registry import SkillConflict, SkillRegistry
from training.skill_contract import TrainingRejected, sign_capture
from training.seed_catalog import inventory_tree
from tests.test_skill_generator_contract import fixture, firewall


def skill():
    return {"schema_version": "skill-v1", "skill_id": "unit-tax", "objective": "Unit skill fixture",
            "steps": ["Read unit input"], "allowed_tools": ["unit_read"]}


class SkillRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SkillRegistry(Path(self.temp.name))

    def test_content_addressed_versions_and_compare_before_update(self):
        first = self.store.publish_diagnostic(skill(), origin_id="unit-first")
        changed = skill()
        changed["steps"].append("Write unit artifact")
        with self.assertRaises(SkillConflict): self.store.publish_diagnostic(changed, origin_id="unit-second")
        second = self.store.publish_diagnostic(changed, origin_id="unit-second", expected_head=first)
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.get("diagnostic", first), skill())
        self.assertEqual(self.store.head("diagnostic", "unit-tax"), second)
        self.assertIsNone(self.store.head("generator", "unit-tax"))

    def test_same_origin_is_idempotent_but_not_rebindable(self):
        first = self.store.publish_diagnostic(skill(), origin_id="unit-first")
        self.assertEqual(first, self.store.publish_diagnostic(skill(), origin_id="unit-first"))
        changed = skill()
        changed["objective"] = "Changed"
        with self.assertRaises(SkillConflict): self.store.publish_diagnostic(changed, origin_id="unit-first", expected_head=first)

    def test_fixture_capture_cannot_publish_generator_skill(self):
        record, key = fixture(), b"unit-only-key" * 4
        with self.assertRaises(TrainingRejected):
            self.store.publish_capture({"record": record, "signature": sign_capture(record, key)}, key, firewall(record["provenance"]))
        self.assertIsNone(self.store.head("generator", "unit-tax"))

    def test_tampered_stored_content_is_rejected(self):
        version = self.store.publish_diagnostic(skill(), origin_id="unit-first")
        with self.store.connect() as connection:
            connection.execute("UPDATE versions SET body=?", ('{"tampered":true}',))
        with self.assertRaises(ValueError): self.store.get("diagnostic", version)


class ModelUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.ledger = ModelUsageLedger(self.path)

    def test_actor_counts_are_transport_observations_without_inference_claims(self):
        generator = self.ledger.begin(actor="generator", model_identity="unit-backend")
        generator.response("unit-response", {"id": "unit-response", "content": "UNIT SECRET NOT TO BE PERSISTED"})
        executor = self.ledger.begin(actor="executor", model_identity="unit-executor")
        executor.failed("UnitTransportError")
        result = self.ledger.summarize()
        self.assertEqual(result["counts_by_actor"]["generator"]["response_received"], 1)
        self.assertEqual(result["counts_by_actor"]["executor"]["failed"], 1)
        self.assertIsNone(result["attested_model_inferences"])
        self.assertIsNone(result["training_updates"])
        self.assertNotIn("UNIT SECRET", "".join(p.read_text() for p in self.path.glob("*.json")))

    def test_lost_reply_stays_started_instead_of_fabricating_failure_or_response(self):
        self.ledger.begin(actor="generator", model_identity="unit-backend")
        self.assertEqual(self.ledger.summarize()["counts_by_actor"]["generator"]["started"], 1)

    def test_terminal_event_cannot_be_counted_twice(self):
        event = self.ledger.begin(actor="generator", model_identity="unit-backend")
        event.failed("UnitError")
        with self.assertRaises(ValueError): event.response("unit-response", {})


class SeedCatalogTests(unittest.TestCase):
    def test_metadata_inventory_has_no_implicit_training_admission(self):
        lines = ["040000 tree " + "a" * 40 + "\t.unit-hidden", "040000 tree " + "b" * 40 + "\tunit-seed"]
        value = inventory_tree(lines, {"unit-seed"}, "c" * 40)
        self.assertEqual(len(value["directories"]), 1)
        self.assertTrue(value["directories"][0]["exact_verified_id_overlap"])
        self.assertEqual(value["admitted_training_seeds"], 0)
        self.assertFalse(value["reference_solutions_read"])

    def test_invalid_tree_or_aliased_identity_rejected(self):
        for line in ("100644 blob " + "a" * 40 + "\tfile", "040000 tree " + "b" * 40 + "\t../outside"):
            with self.subTest(line=line), self.assertRaises(ValueError): inventory_tree([line], set(), "c" * 40)


if __name__ == "__main__": unittest.main()
