"""Network inventory can change outside the project's allocation lock."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from integrations.environments.network import create_network


class NetworkAllocationTests(unittest.TestCase):
    def test_removed_foreign_network_keeps_remaining_subnets_reserved(self):
        commands = []
        def run(argv, **kwargs):
            commands.append(argv)
            if argv[:3] == ("docker", "network", "ls"):
                return SimpleNamespace(returncode=0, stdout="still-present\nalready-gone\n", stderr="")
            if argv[:3] == ("docker", "network", "inspect"):
                return SimpleNamespace(returncode=1,
                    stdout=json.dumps([{"IPAM": {"Config": [{"Subnet": "10.240.0.0/28"}]}}]),
                    stderr="Error response from daemon: network already-gone not found\n")
            if argv[:3] == ("ip", "-j", "route"):
                return SimpleNamespace(returncode=0, stdout="[]", stderr="")
            return SimpleNamespace(returncode=0, stdout="new-network-id", stderr="")
        with patch("integrations.environments.network.subprocess.run", side_effect=run):
            selected = create_network("fixture-owned", internal=True)
        self.assertEqual(selected, "10.240.0.16/28")
        self.assertIn("--internal", commands[-1])

    def test_inventory_permission_error_is_not_ignored(self):
        def run(argv, **kwargs):
            if argv[:3] == ("docker", "network", "ls"):
                return SimpleNamespace(returncode=0, stdout="existing\n", stderr="")
            return SimpleNamespace(returncode=1, stdout="[]", stderr="permission denied")
        with patch("integrations.environments.network.subprocess.run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                create_network("fixture-owned", internal=False)

    def test_all_listed_networks_removed_before_inspection(self):
        def run(argv, **kwargs):
            if argv[:3] == ("docker", "network", "ls"):
                return SimpleNamespace(returncode=0, stdout="gone\n", stderr="")
            if argv[:3] == ("docker", "network", "inspect"):
                return SimpleNamespace(returncode=1, stdout="[]", stderr="Error response from daemon: network gone not found\n")
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        with patch("integrations.environments.network.subprocess.run", side_effect=run):
            self.assertEqual(create_network("fixture-owned", internal=False), "10.240.0.0/28")
