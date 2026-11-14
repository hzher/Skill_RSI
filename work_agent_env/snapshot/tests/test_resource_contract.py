import unittest

from training.resource_contract import resource_profile, validate_resource_provenance, NEMO_PIN
from training.launch_verl import command


class GeneratorResourceContractTests(unittest.TestCase):
    def test_shared_mounted_isolated_and_standalone_isolated_routes_are_explicit(self):
        shared = resource_profile("shared", "toolathlon_resources")
        mounted = resource_profile("isolated", "toolathlon_resources")
        standalone = resource_profile("isolated", "isolated_toolathlon_resources")
        self.assertEqual(shared["route_prefix"], "")
        self.assertEqual(mounted["route_prefix"], "/isolated")
        self.assertEqual(standalone["route_prefix"], "")
        self.assertNotEqual(shared["environment_version"], mounted["environment_version"])
        self.assertEqual(mounted["environment_version"], standalone["environment_version"])
        self.assertFalse(mounted["training_parallelism_enabled"])
        with self.assertRaises(ValueError): resource_profile("shared", "isolated_toolathlon_resources")

    def test_live_backend_framework_or_environment_mismatch_is_rejected(self):
        profile = resource_profile("isolated", "toolathlon_resources")
        provenance = {"framework": "nemo_gym", "source_commit": NEMO_PIN, "resource_backend": "isolated", "environment_version": profile["environment_version"]}
        self.assertEqual(validate_resource_provenance(profile, provenance), profile["environment_version"])
        for field, value in (("framework", "unit-fake"), ("resource_backend", "shared"), ("source_commit", "b" * 40), ("environment_version", "other")):
            with self.assertRaises(ValueError): validate_resource_provenance(profile, {**provenance, field: value})

    def test_launch_command_changes_only_the_resource_sampling_configuration(self):
        shared, isolated = command(), command("isolated")
        self.assertEqual(isolated[:-1], shared)
        self.assertIn("nemo_generator_isolated.yaml", isolated[-1])
        self.assertNotIn("--execute", isolated)
        with self.assertRaises(ValueError): command("verified")
