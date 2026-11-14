import hashlib
import json
import unittest
from unittest.mock import patch

from training.seed_capabilities import configuration, decode_batch, object_inventory, summarize, fetch_configuration_objects


class SeedCapabilityTests(unittest.TestCase):
    def test_only_service_and_tool_names_leave_public_configuration_parser(self):
        raw = json.dumps({"needed_mcp_servers": ["woocommerce", "google_sheet", "woocommerce"], "needed_local_tools": ["terminal"],
                          "meta": {"private_canary": "DO_NOT_EXPORT", "instruction": "DO_NOT_EXPORT"}}).encode()
        row = configuration(raw)
        self.assertEqual(row["needed_mcp_servers"], ["google_sheet", "woocommerce"])
        self.assertNotIn("DO_NOT_EXPORT", json.dumps(row))

    def test_missing_or_unstructured_declarations_are_unknown_instead_of_empty_capabilities(self):
        for value in ({}, {"needed_mcp_servers": ["https://unit.invalid/private"], "needed_local_tools": []},
                      {"needed_mcp_servers": {}, "needed_local_tools": []}):
            with self.assertRaises(ValueError): configuration(json.dumps(value).encode())

    def test_batch_requires_exact_git_content_identity_and_complete_frames(self):
        body = b'{"unit_metadata":true}'
        oid = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
        raw = f"{oid} blob {len(body)}\n".encode() + body + b"\n"
        self.assertEqual(decode_batch(raw, {oid: len(body)})[oid], body)
        for bad in (raw[:-1], raw + b'extra', raw.replace(b'true', b'fake')):
            with self.assertRaises(ValueError): decode_batch(bad, {oid: len(body)})

    def test_tree_counts_assets_and_script_existence_without_loading_any_body(self):
        def entry(path, mode="100644"): return f"{mode} blob {'a'*40}\ttasks/finalpool/unit/{path}\0".encode()
        root_metadata = f"100644 blob {'b'*40}\ttasks/finalpool/task_conflict.json\0".encode()
        rows = object_inventory(root_metadata + entry("task_config.json") + entry("evaluation/main.py", "100755") + entry("files/unit.txt"), {"unit"})
        self.assertTrue(rows["unit"]["evaluation_script_present"])
        self.assertEqual(rows["unit"]["file_asset_count"], 1)
        self.assertFalse(rows["unit"]["preprocess_script_present"])
        with self.assertRaises(ValueError): object_inventory(entry("task_config.json", "120000"), {"unit"})

    def test_service_frequency_does_not_admit_or_claim_runnable_task(self):
        rows = {"unit": {"configuration_status": "inspected", "needed_mcp_servers": ["woocommerce"], "needed_local_tools": []},
                "missing": {"configuration_status": "unknown_missing_local_git_object"}}
        summary = summarize(rows)
        self.assertEqual(summary["configurations_inspected"], 1)
        self.assertEqual(summary["configuration_unknown"], 1)
        self.assertEqual(summary["admitted_training_seeds"], 0)
        self.assertEqual(summary["complete_task_adapters"], [])

    def test_explicit_metadata_fetch_refuses_an_unlocked_remote_before_network_request(self):
        with patch("training.seed_capabilities.subprocess.check_output", return_value=b"https://unit.invalid/unlocked.git\n"), \
             patch("training.seed_capabilities.subprocess.run") as fetch:
            with self.assertRaisesRegex(ValueError, "remote differs"):
                fetch_configuration_objects("unit-checkout", {"url": "https://unit.invalid/locked.git"}, ["a" * 40])
            fetch.assert_not_called()
