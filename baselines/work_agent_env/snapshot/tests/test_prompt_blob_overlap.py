import unittest

from training.prompt_blob_overlap import prompt_entries, compare_prompts, pool_entries


class PromptObjectMetadataTests(unittest.TestCase):
    def entry(self, task, oid="a" * 40, mode="100644"):
        return f"{mode} blob {oid}\ttasks/finalpool/{task}/docs/task.md\0".encode()

    def test_metadata_records_prompt_identity_without_loading_any_blob_body(self):
        rows = prompt_entries(self.entry("unit-one"), {"unit-one"})
        self.assertEqual(rows["unit-one"]["git_blob"], "a" * 40)
        self.assertEqual(set(rows["unit-one"]), {"path", "git_blob"})
        alternate = self.entry("unit-one").replace(b"docs/task.md", b"initial_workspace/task.md")
        self.assertEqual(prompt_entries(alternate, {"unit-one"})["unit-one"]["git_blob"], "a" * 40)
        both = prompt_entries(self.entry("unit-one") + alternate.replace(b"a" * 40, b"b" * 40), {"unit-one"})
        self.assertEqual(both["unit-one"]["additional_prompt_objects"][0]["git_blob"], "b" * 40)

    def test_linked_ambiguous_missing_or_traversing_prompt_paths_are_rejected(self):
        for raw in (self.entry("unit", mode="120000"), self.entry("unit") * 2, self.entry("../outside"),
                    self.entry("unit").replace(b"docs/task.md", b"arbitrary/task.md"), b""):
            with self.assertRaises(ValueError): prompt_entries(raw, {"unit"})

    def test_identical_prompt_objects_detect_renamed_tasks_without_admitting_anything(self):
        gym = {"gym-unit": {"git_blob": "a" * 40}, "different-unit": {"git_blob": "b" * 40}}
        verified = {"heldout-renamed-unit": {"git_blob": "a" * 40}}
        result = compare_prompts(gym, verified)
        self.assertEqual(result["exact_prompt_blob_overlaps"], {"gym-unit": ["heldout-renamed-unit"]})
        self.assertEqual(result["automatic_admissions"], 0)
        self.assertEqual(result["semantic_independence"], "unknown_requires_family_alias_review")
        self.assertFalse(result["verified_task_bodies_read"])
        verified["heldout-renamed-unit"] = {"git_blob": "c" * 40, "additional_prompt_objects": [{"git_blob": "a" * 40}]}
        self.assertEqual(compare_prompts(gym, verified)["overlapping_gym_tasks"], 1)

    def test_root_metadata_files_are_explicitly_reported_without_becoming_tasks(self):
        raw = (f"040000 tree {'a'*40}\tunit\0" + f"100644 blob {'b'*40}\ttask_conflict.json\0").encode()
        directories, metadata = pool_entries(raw, {"unit", "task_conflict.json"})
        self.assertEqual(directories, {"unit"})
        self.assertEqual(metadata, {"task_conflict.json": "b" * 40})
        with self.assertRaises(ValueError): pool_entries(raw, {"unit"})
