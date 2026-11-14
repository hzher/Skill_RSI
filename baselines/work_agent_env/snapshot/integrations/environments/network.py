"""Create small, isolated task subnets without consuming daemon-wide default pools."""
import fcntl
import ipaddress
import json
import os
import re
from pathlib import Path
import subprocess


def create_network(name, *, internal, timeout=60):
    def run(*arguments, check=True):
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError("Network operation failed: " + result.stderr[-1000:])
        return result
    lock = Path("/tmp") / ("work-agent-network-" + str(os.getuid()) + ".lock")
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        for attempt in range(3):
            identifiers = run("docker", "network", "ls", "-q").stdout.split()
            networks = []
            if identifiers:
                inspection = run("docker", "network", "inspect", *identifiers, check=False)
                if inspection.returncode:
                    errors = [line.strip() for line in inspection.stderr.splitlines() if line.strip()]
                    # Other workloads do not hold our lock. Docker returns the
                    # surviving networks as JSON even when a listed network vanished.
                    if not errors or any(not re.fullmatch(
                            r"Error response from daemon: network \S+ not found", line) for line in errors):
                        raise RuntimeError("Network operation failed: " + inspection.stderr[-1000:])
                    if not inspection.stdout.strip():
                        continue  # Re-list instead of assuming the remaining inventory is empty.
                networks = json.loads(inspection.stdout)
            occupied = [ipaddress.ip_network(item["Subnet"], strict=False) for network in networks
                for item in network.get("IPAM", {}).get("Config") or [] if item.get("Subnet")]
            routes = json.loads(run("ip", "-j", "route").stdout)
            occupied += [ipaddress.ip_network(route["dst"], strict=False) for route in routes if route.get("dst", "default") != "default"]
            subnet = next((candidate for octet in range(240, 255)
                for candidate in ipaddress.ip_network(f"10.{octet}.0.0/16").subnets(new_prefix=28)
                if not any(candidate.overlaps(existing) for existing in occupied if existing.version == 4)), None)
            if subnet is None:
                raise RuntimeError("No non-overlapping task subnet is available")
            arguments = ["docker", "network", "create", "--subnet", str(subnet)]
            if internal:
                arguments.append("--internal")
            result = run(*arguments, name, check=False)
            if not result.returncode:
                return str(subnet)
            if "overlap" not in result.stderr.lower():
                raise RuntimeError("Task network creation failed: " + result.stderr[-1000:])
        raise RuntimeError("Concurrent network allocation could not be resolved")
