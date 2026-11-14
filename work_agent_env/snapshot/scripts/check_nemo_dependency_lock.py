"""Compare actual Linux installed distribution versions with the exact CPU lock."""
import hashlib
import json
import subprocess
import sys

from nemo_vm import ROOT, ssh_arguments


def main():
    path = ROOT / "configs/nemo_linux_runtime.requirements.txt"
    expected = dict(line.split("==", 1) for line in path.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#"))
    code = "import importlib.metadata,json\nprint(json.dumps({d.metadata['Name']:d.version for d in importlib.metadata.distributions()}))\n"
    command = [str(ROOT / ".runtime/nemo-venv/bin/python"), "-"] if sys.platform == "linux" else ssh_arguments() + ["/home/workenv/nemo-venv/bin/python -"]
    completed = subprocess.run(command, input=code.encode(), capture_output=True, timeout=40, check=True)
    actual = json.loads(completed.stdout)
    mismatches = {name: {"expected": version, "actual": actual.get(name)} for name, version in expected.items() if actual.get(name) != version}
    result = {"schema_version": "nemo-linux-dependency-lock-check-v1", "passed": not mismatches,
              "packages_checked": len(expected), "mismatches": mismatches, "runtime_reinstalled": False,
              "lock_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "real_model_calls": 0, "training_updates": 0,
              "execution_host": "native_linux" if sys.platform == "linux" else "windows_managed_linux_vm"}
    (ROOT / "evidence/mainline/dependency_lock.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
