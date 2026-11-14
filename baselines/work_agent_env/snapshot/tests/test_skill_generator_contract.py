"""Synthetic unit fixtures test admission guards; they are never training data."""
from copy import deepcopy
import hashlib
import unittest

from evaluation.firewall import ContaminationError, EvaluationFirewall, digest
from training.skill_contract import (REWARD_VERSION, SCHEMA, TrainingRejected, admit_capture,
                                     canonical, pack_generator_trace, recipe_payload, sign_capture, validate_skill)


def fixture():
    provenance = {"source": "toolathlon_gym", "split": "train", "task_id": "unit-train", "task_family": "unit-train-family",
                  "source_commit": "a" * 40, "seed_sha256": "b" * 64, "prompt_sha256": "e" * 64, "ancestor_tasks": [], "ancestor_families": []}
    prompt = [{"role": "user", "actor": "context", "token_ids": [11, 12]}]
    downstream = {**provenance, "task_id": "unit-downstream", "task_family": "unit-downstream-family",
                  "seed_sha256": "d" * 64, "prompt_sha256": "f" * 64}
    skill = {"schema_version": "skill-v1", "skill_id": "unit", "objective": "unit objective",
             "steps": ["Read relevant data"], "allowed_tools": ["unit_read"]}
    skill_sha = hashlib.sha256(canonical(skill)).hexdigest()
    return {"schema_version": SCHEMA, "training_target": "skill_generator", "provenance": provenance,
            "identities": {"generator_policy_id": "generator-unit", "generator_model_revision": "unit-model",
                           "tokenizer_revision": "unit-tokenizer", "fixed_executor_id": "executor-unit", "environment_version": "unit-env"},
            "measurement": {"reward": 1.0, "mask_sample": False, "evaluation_status": "completed", "reward_version": REWARD_VERSION,
                            "evaluation_ref": "unit-evaluation", "executor_trace_ref": "unit-executor-trace"},
            "generated_skill": skill, "generated_skill_sha256": skill_sha, "generation_status": "valid_skill",
            "downstream_execution": {"provenance": downstream, "task_id": downstream["task_id"],
                                     "task_prompt_sha256": "1" * 64, "skill_sha256": skill_sha,
                                     "executor_id": "executor-unit", "evaluation_ref": "unit-evaluation",
                                     "executor_trace_ref": "unit-executor-trace"},
            "capture": {"kind": "unit_fixture", "model_requests": 0, "capture_id": "unit-only", "provider_request_id": "unit-only"},
            "input_message_log": prompt, "message_log": prompt + [
                {"role": "assistant", "actor": "generator", "policy_id": "generator-unit", "token_ids": [31, 32], "generation_logprobs": [-.1, -.2]},
                {"role": "tool", "actor": "context", "token_ids": [41]},
                {"role": "assistant", "actor": "generator", "policy_id": "generator-unit", "token_ids": [33], "generation_logprobs": [-.3]}]}


def firewall(seed, *additional):
    manifest = {"schema_version": "toolathlon-verified-metadata-v1", "source": "toolathlon_verified", "commit": "c" * 40,
                "task_ids": ["unit-heldout"], "family_roots": ["unit-heldout-family"]}
    registry = {"schema_version": "toolathlon-training-seeds-v1", "evaluation_manifest_sha256": digest(manifest),
                "admitted_seeds": [seed, *additional]}
    return EvaluationFirewall(manifest, registry)


class GeneratorContractTests(unittest.TestCase):
    def pack(self, row=None, **limits):
        return pack_generator_trace(row or fixture(), prompt_limit=limits.get("prompt_limit", 8), response_limit=limits.get("response_limit", 8))

    def test_original_tokens_and_generator_only_loss(self):
        packed = self.pack()
        self.assertEqual(packed.prompt_ids, [11, 12])
        self.assertEqual(packed.response_ids, [31, 32, 41, 33])
        self.assertEqual(packed.response_mask, [1, 1, 0, 1])
        self.assertEqual(packed.response_logprobs, [-.1, -.2, 0, -.3])

    def test_executor_in_trace_is_rejected_before_recipe(self):
        row = fixture()
        row["message_log"][1]["actor"] = "executor"
        with self.assertRaises(TrainingRejected): self.pack(row)

    def test_downstream_task_skill_and_receipts_cannot_be_rebound(self):
        for section, key, value in (
            ("downstream_execution", "task_id", "unit-train"),
            ("downstream_execution", "evaluation_ref", "another-evaluation"),
            ("downstream_execution", "executor_trace_ref", "another-trace"),
            ("downstream_execution", "skill_sha256", "0" * 64),
        ):
            row = fixture()
            row[section][key] = value
            with self.subTest(key=key), self.assertRaises(TrainingRejected): self.pack(row)
        row = fixture()
        row["downstream_execution"]["provenance"] = row["provenance"]
        with self.assertRaises(TrainingRejected): self.pack(row)

    def test_missing_logprobs_are_not_filled_with_zero(self):
        for bad in (None, [], [-.1], [float("nan"), -.2], [.1, -.2], [False, -.2]):
            row = fixture()
            row["message_log"][1]["generation_logprobs"] = bad
            with self.subTest(bad=bad), self.assertRaises(TrainingRejected): self.pack(row)

    def test_unknown_is_excluded_but_measured_failure_is_eligible(self):
        for value in (None, float("nan"), True, 2):
            row = fixture()
            row["measurement"]["reward"] = value
            with self.subTest(value=value), self.assertRaises(TrainingRejected): self.pack(row)
        row = fixture()
        row["measurement"]["reward"] = 0
        self.assertEqual(self.pack(row).reward, 0)
        row["measurement"]["mask_sample"] = True
        with self.assertRaises(TrainingRejected): self.pack(row)

    def test_prefix_or_policy_tampering_is_rejected(self):
        row = fixture()
        row["input_message_log"] = deepcopy(row["input_message_log"])
        row["input_message_log"][0]["token_ids"] = [999]
        with self.assertRaises(TrainingRejected): self.pack(row)
        row = fixture()
        row["message_log"][1]["policy_id"] = "executor-unit"
        with self.assertRaises(TrainingRejected): self.pack(row)

    def test_oversize_skill_is_rejected_without_truncation(self):
        with self.assertRaises(TrainingRejected): self.pack(response_limit=3)
        with self.assertRaises(TrainingRejected): self.pack(prompt_limit=1)

    def test_fixture_never_admitted_even_with_valid_signature(self):
        row = fixture()
        key = b"unit-test-only-key" * 3
        with self.assertRaises(TrainingRejected):
            admit_capture(row, sign_capture(row, key), key, firewall=firewall(row["provenance"]))

    def test_authentication_detects_changed_outcome(self):
        row = fixture()
        key = b"unit-test-only-key" * 3
        signature = sign_capture(row, key)
        row["measurement"]["reward"] = 0
        with self.assertRaisesRegex(TrainingRejected, "authentication"):
            admit_capture(row, signature, key, firewall=firewall(row["provenance"]))

    def test_recipe_projection_preserves_loss_boundary_and_detects_mutation(self):
        row = fixture()
        packed = self.pack(row)
        result = recipe_payload(row, packed)
        self.assertEqual(result["message_log"][1]["token_ids"], [31, 32])
        self.assertEqual(result["full_result"]["reward"], 1)
        self.assertEqual(result["downstream_execution"]["executor_trace_ref"], "unit-executor-trace")
        self.assertNotIn("unit-executor-trace", str(result["message_log"]))
        row["message_log"][1]["token_ids"][0] = 999
        with self.assertRaises(TrainingRejected): recipe_payload(row, packed)

    def test_skill_is_bounded_and_declarative(self):
        skill = {"schema_version": "skill-v1", "skill_id": "unit", "objective": "unit objective", "steps": ["Read relevant data"], "allowed_tools": ["unit_read"]}
        self.assertEqual(validate_skill(skill), skill)
        skill["steps"] = ["x"] * 33
        with self.assertRaises(TrainingRejected): validate_skill(skill)

    def test_provenance_and_model_revision_cannot_change_after_packing(self):
        for section, key in (("provenance", "source_commit"), ("identities", "generator_model_revision")):
            row = fixture()
            packed = self.pack(row)
            row[section][key] = "changed-after-admission"
            with self.subTest(section=section), self.assertRaises(TrainingRejected): recipe_payload(row, packed)


class FirewallTests(unittest.TestCase):
    def test_independently_admitted_metadata(self):
        row = fixture()["provenance"]
        self.assertTrue(firewall(row).check_training_record(row))

    def test_exact_and_parent_family_leakage(self):
        original = fixture()["provenance"]
        for patch in ({"task_id": "unit-heldout"}, {"ancestor_tasks": ["unit-heldout"]},
                      {"task_family": "unit-heldout-family"}, {"ancestor_families": ["unit-heldout-family"]}):
            row = {**original, **patch}
            with self.subTest(patch=patch), self.assertRaises(ContaminationError): firewall(row).check_training_record(row)

    def test_verified_source_and_answer_fields_never_admitted(self):
        original = fixture()["provenance"]
        for patch in ({"source": "toolathlon_verified"}, {"split": "dev"}, {"reference_solution": "no"}, {"seed_sha256": "d" * 64}):
            with self.subTest(patch=patch), self.assertRaises(ContaminationError):
                firewall(original).check_training_record({**original, **patch})

    def test_unreviewed_seed_rejected(self):
        row = fixture()["provenance"]
        guard = firewall(row)
        guard.allowed.clear()
        with self.assertRaises(ContaminationError): guard.check_training_record(row)

    def test_evaluation_requires_frozen_artifact_and_unique_pinned_tasks(self):
        guard = firewall(fixture()["provenance"])
        for tasks in ([], ["unknown"], ["unit-heldout", "unit-heldout"]):
            with self.assertRaises(ContaminationError):
                guard.check_evaluation(tasks, model_checkpoint="unit-checkpoint", training_registry_sha256="unit", expected_registry_sha256="unit")
        self.assertTrue(guard.check_evaluation(["unit-heldout"], model_checkpoint="unit-checkpoint", training_registry_sha256="unit", expected_registry_sha256="unit"))


if __name__ == "__main__": unittest.main()
