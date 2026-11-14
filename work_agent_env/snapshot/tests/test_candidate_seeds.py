from copy import deepcopy
import unittest
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from training.candidate_seeds import build_candidate, read_pinned_candidate, preserve_candidate
from evaluation.firewall import ContaminationError
from tests.test_skill_generator_contract import firewall


class CandidateSeedTests(unittest.TestCase):
    def test_candidate_identity_uses_git_bytes_independently_of_checkout_line_endings(self):
        instruction, config = b"Unit public instruction\n", b'{"unit_metadata":true}\n'
        metadata = {"git_blob": "a" * 40}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unit-working-copy.json").write_bytes(config.replace(b"\n", b"\r\n"))
            with patch("training.candidate_seeds.pinned_blob", side_effect=[(instruction, metadata), (config, metadata)]):
                candidate = read_pinned_candidate(root, {"commit": "b" * 40})
            self.assertEqual(candidate["source_identity"]["task_config_sha256"], hashlib.sha256(config).hexdigest())
            self.assertEqual(candidate["training_row"]["task_prompt"], instruction.decode())
            self.assertEqual(candidate["source_read_mode"], "pinned_git_blob_bytes")
            self.assertFalse(candidate["verified_task_bodies_read"])

    def test_prior_candidate_is_archived_byte_exactly_and_corrupt_archive_is_not_overwritten(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "candidate.json"
            raw = json.dumps(build_candidate("Unit public task", "a" * 40, "b" * 64, "c" * 64)).encode()
            path.write_bytes(raw)
            archive = root / preserve_candidate(root, path)
            self.assertEqual(archive.read_bytes(), raw)
            self.assertEqual(preserve_candidate(root, path), archive.relative_to(root).as_posix())
            archive.write_bytes(b'corrupt unit archive')
            with self.assertRaises(ValueError): preserve_candidate(root, path)
            self.assertEqual(path.read_bytes(), raw)

    def test_public_instruction_is_bound_and_stays_quarantined(self):
        result = build_candidate("Unit public task", "a" * 40, "b" * 64, "c" * 64)
        self.assertEqual(result["candidate_status"], "quarantined_not_admitted")
        self.assertFalse(result["reference_solutions_read"])
        self.assertFalse(result["verified_task_bodies_read"])
        row = result["training_row"]
        self.assertEqual(row["provenance"]["split"], "candidate")
        self.assertEqual(row["task_prompt"], "Unit public task")
        guard = firewall(deepcopy(row["provenance"]))
        with self.assertRaises(ContaminationError): guard.check_training_record(row["provenance"])

    def test_source_byte_changes_change_seed_identity(self):
        first = build_candidate("Unit public task", "a" * 40, "b" * 64, "c" * 64)
        second = build_candidate("Unit changed task", "a" * 40, "d" * 64, "c" * 64)
        self.assertNotEqual(first["training_row"]["provenance"]["seed_sha256"], second["training_row"]["provenance"]["seed_sha256"])
        self.assertNotEqual(first["training_row"]["provenance"]["prompt_sha256"], second["training_row"]["provenance"]["prompt_sha256"])


if __name__ == "__main__": unittest.main()
