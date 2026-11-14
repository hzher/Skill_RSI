from copy import deepcopy
import unittest

from training.candidate_seeds import build_candidate
from training.lineage_review import evaluation_diff, prepare_review
from evaluation.firewall import digest


def manifest():
    return {"schema_version": "toolathlon-verified-metadata-v1", "source": "toolathlon_verified", "commit": "a" * 40,
            "task_ids": ["unit-heldout"], "family_roots": ["unit-heldout-family"]}


class LineageReviewTests(unittest.TestCase):
    def blob_audit(self):
        return {"schema_version": "task-prompt-git-blob-comparison-v1", "evaluation_manifest_sha256": digest(manifest()),
                "gym_commit": "b" * 40, "verified_commit": "a" * 40,
                "exact_prompt_blob_overlaps": {"wc-sales-tax-summary": ["unit-heldout"]}}

    def test_prompt_object_match_marks_candidate_inadmissible(self):
        candidate = build_candidate("Unit task", "b" * 40, "c" * 64, "d" * 64)
        review = prepare_review(candidate, manifest(), self.blob_audit())
        self.assertEqual(review["status"], "exact_prompt_overlap_not_admissible")
        self.assertEqual(review["exact_prompt_blob_overlap_tasks"], ["unit-heldout"])
        self.assertFalse(review["training_admission"])

    def test_prompt_comparison_from_another_source_or_manifest_is_rejected(self):
        candidate = build_candidate("Unit task", "b" * 40, "c" * 64, "d" * 64)
        for key in ("gym_commit", "verified_commit", "evaluation_manifest_sha256"):
            report = self.blob_audit()
            report[key] = "f" * 64
            with self.assertRaises(ValueError): prepare_review(candidate, manifest(), report)

    def test_no_exact_overlap_never_automatically_admits_candidate(self):
        candidate = build_candidate("Unit task", "b" * 40, "c" * 64, "d" * 64)
        review = prepare_review(candidate, manifest())
        self.assertFalse(review["exact_id_overlap"])
        self.assertFalse(review["training_admission"])
        self.assertFalse(review["alias_lineage_review_complete"])

    def test_same_ids_new_upstream_pin_invalidates_prior_reviews(self):
        old, new = manifest(), manifest()
        new["commit"] = "b" * 40
        result = evaluation_diff(old, new, [])
        self.assertTrue(result["all_admitted_reviews_require_refresh"])
        self.assertEqual(result["added_tasks"], [])
        self.assertEqual(result["automatic_admissions"], 0)

    def test_new_family_flags_previously_admitted_ancestor(self):
        old, new = manifest(), manifest()
        new["family_roots"].append("unit-parent")
        seed = {"task_id": "unit-child", "task_family": "unit-train", "ancestor_families": ["unit-parent"], "seed_sha256": "c" * 64}
        result = evaluation_diff(old, new, [seed])
        self.assertEqual(result["conflicting_admitted_seed_hashes"], ["c" * 64])


if __name__ == "__main__": unittest.main()
