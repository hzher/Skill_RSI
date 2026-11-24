"""Verify the four frozen skill packages and exercise their public interfaces offline."""
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
EXPECTED = {
    "reuse-observed-schema": {"SKILL.md"},
    "read-source-in-context": {"SKILL.md"},
    "recall-observed-data-map": {
        "SKILL.md", "references/integration.md", "scripts/gateway.py", "scripts/host.py", "scripts/routes.py",
    },
    "audit-structured-decision-logs": {"SKILL.md", "scripts/audit.py", "scripts/public_cli.py"},
}


def check(condition, message):
    if not condition:
        raise ValueError(message)


def verify_files():
    packages = json.loads((ROOT / "evidence.json").read_bytes())["packages"]
    check(set(packages) == set(EXPECTED), "Unexpected package list in evidence.json")
    for name, expected in EXPECTED.items():
        package = ROOT / name
        check(package.is_dir() and not package.is_symlink(), f"Missing package or unexpected link: {name}")
        declared = packages[name]["files"]
        check(set(declared) == expected, f"Unexpected declared files: {name}")
        observed = {}
        for path in package.rglob("*"):
            relative = path.relative_to(package)
            if "__pycache__" in relative.parts:
                continue
            check(not path.is_symlink(), f"Unexpected symlink: {name}/{relative}")
            if path.is_file():
                observed[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        check(observed == declared, f"Package file set or SHA-256 differs: {name}")
        manifest = json.dumps(observed, sort_keys=True, separators=(",", ":")).encode("utf-8")
        check(hashlib.sha256(manifest).hexdigest() == packages[name]["package_sha256"],
              f"Package manifest SHA-256 differs: {name}")


def audit_stream(side):
    """Synthetic fixture used at the original portable package seal."""
    amount, chips = (8, 2) if side == "A" else (9, -2)
    coverage = "complete" if side == "A" else "gap_before_first_action"
    events = [("capture_attestation", {"actor_input_coverage": coverage})]
    if side == "A":
        events.append(("actor_input", {"result": {"payload": {"metadata": {"strategy": "自建类别"}}}}))
    action = {"call_id": "turn-01", "kind": "raise", "amount": amount, "cached": False}
    receipt = {"receipt_id": "local-receipt", "net_chips": chips}
    events.extend([
        ("accepted_act", action), ("accepted_act", {**action, "cached": True}),
        ("settlement", receipt), ("archiver_replay", receipt),
    ])
    return [
        {"attempt": "dummy-" + side, "thread": "local-" + side, "seq": seq, "event": event, "payload": payload}
        for seq, (event, payload) in enumerate(events, 1)
    ]


def verify_audit_cli():
    cli = ROOT / "audit-structured-decision-logs/scripts/public_cli.py"
    arms = {side: audit_stream(side) for side in ("A", "B")}
    packet = {"cases": [{"case_id": "自建案例", "arms": arms}]}

    def invoke():
        return subprocess.run(
            [sys.executable, "-X", "utf8", "-I", "-B", str(cli)],
            input=json.dumps(packet).encode("utf-8"), capture_output=True, timeout=15,
        )

    expected = {
        "A_at_first": "yes", "B_at_first": "unknown",
        "A_first_affected_action": 1, "B_first_affected_action": None,
        "A_net_chips": 2, "B_net_chips": -2, "first_divergence": 1,
        "identical_first_action": False, "adoption_causal": "unknown",
    }
    good = invoke()
    check(good.returncode == 0 and not good.stderr and json.loads(good.stdout) == {"自建案例": expected},
          "Audit CLI did not reproduce the nine expected fields")
    del arms["B"][-2:]  # Remove the settlement and its replay.
    missing = invoke()
    check(missing.returncode == 2 and not missing.stdout and missing.stderr.startswith(b"evidence_gap:"),
          "Audit CLI did not report the missing settlement")


def verify_notebook():
    package = ROOT / "recall-observed-data-map"
    host = runpy.run_path(str(package / "scripts/host.py"))
    gateway = host["load_gateway"](package)
    book = gateway.Notebook()
    check(gateway.appendix(book.payload()) == "", "Empty notebook produced an appendix")
    ddl = "CREATE TABLE items (item_id INTEGER, category TEXT)"
    observation = "Synthetic query result\n\nname | sql\n-----+----\nitems | " + ddl
    book.observe("SELECT name, sql FROM sqlite_master WHERE type='table'", observation, "Q1/act-2")
    values = book.payload()
    expected = {"table": "items", "ddl": ddl, "columns": [["item_id", "INTEGER"], ["category", "TEXT"]],
                "from": "Q1/act-2"}
    check(values["fields"] == [expected], "Observed schema or source reference differs")
    appendix = gateway.appendix(values)
    check("items" in appendix and "Q1/act-2" in appendix, "Appendix lost the table or its source")
    check(gateway.appendix(gateway.Notebook().payload()) == "", "State leaked into a fresh notebook")


if __name__ == "__main__":
    verify_files()
    verify_audit_cli()
    verify_notebook()
    print("Verified four packages, both audit CLI paths, and notebook isolation.")
