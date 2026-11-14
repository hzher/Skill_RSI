import unittest

from integrations.runtime_identity import BRIDGE_FILES, NEMO_FILE, NEMO_EXTENSIONS, classify


class RuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.expected = {name: "a" * 64 for name in (*BRIDGE_FILES, NEMO_FILE, *NEMO_EXTENSIONS)}
        self.provenance = {"framework": "nemo_gym", "source_commit": "b" * 40, "loaded_adapter_sha256": "a" * 64}
        self.provenance["loaded_extension_sha256"] = {name: "a" * 64 for name in NEMO_EXTENSIONS}
        self.bridge = {"resource_backend": "original_toolathlon", "active_session": None,
                       "loaded_source_sha256": {name: "a" * 64 for name in BRIDGE_FILES}}

    def test_matching_loaded_adapters_are_ready(self):
        self.assertTrue(classify(self.provenance, self.bridge, self.expected, "b" * 40)["ready"])

    def test_new_disk_source_does_not_relabel_old_loaded_process(self):
        self.expected[NEMO_FILE] = "c" * 64
        result = classify(self.provenance, self.bridge, self.expected, "b" * 40)
        self.assertFalse(result["ready"])
        self.assertFalse(result["loaded_nemo_source_matches"])

    def test_unknown_or_missing_loaded_identity_is_not_ready(self):
        del self.provenance["loaded_adapter_sha256"]
        self.assertFalse(classify(self.provenance, self.bridge, self.expected, "b" * 40)["ready"])
        self.assertFalse(classify(None, self.bridge, self.expected, "b" * 40)["ready"])

    def test_foreign_framework_or_helper_revision_is_not_ready(self):
        self.provenance["framework"] = "other"
        self.assertFalse(classify(self.provenance, self.bridge, self.expected, "b" * 40)["ready"])
        self.provenance["framework"] = "nemo_gym"
        self.bridge["loaded_source_sha256"][BRIDGE_FILES[1]] = "c" * 64
        self.assertFalse(classify(self.provenance, self.bridge, self.expected, "b" * 40)["ready"])

    def test_active_synthetic_task_blocks_reload_and_extension_bytes_are_bound(self):
        self.bridge["synthetic"] = {"active_sessions": ["active-synthetic-episode"]}
        self.assertEqual(classify(self.provenance, self.bridge, self.expected, "b" * 40)["active_session"], "active-synthetic-episode")
        self.expected[NEMO_EXTENSIONS[0]] = "c" * 64
        self.assertFalse(classify(self.provenance, self.bridge, self.expected, "b" * 40)["ready"])


if __name__ == "__main__": unittest.main()
