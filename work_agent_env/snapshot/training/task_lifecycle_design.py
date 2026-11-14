"""Inspect only allowlisted public seed implementation blobs; execute no task code."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from workenv.core import contained, save

ROOT = Path(__file__).resolve().parents[1]
TASK = "howtocook-meal-plan-gcal"
MEMBERS = {
    "preprocess": "tasks/finalpool/" + TASK + "/preprocess/main.py",
    "evaluation": "tasks/finalpool/" + TASK + "/evaluation/main.py",
    "calendar_service": "local_servers/Calendar-Autoauth-MCP-Server/src/index.ts",
    "recipe_data_loader": "local_servers/HowToCook-mcp/src/data/recipes.ts",
}
MAX_BYTES = 256 * 1024
PUBLIC_RECIPE_MEMBER = "local_servers/HowToCook-mcp/src/data/all_recipes.json"


def python_contract(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES: raise ValueError("Public source exceeds byte budget")
    tree = ast.parse(raw.decode("utf-8-sig"))
    environments, tables, artifacts, imports = set(), set(), set(), set()
    fixed_port = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import): imports.update(alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module: imports.add(node.module.split(".")[0])
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}\.xlsx", node.value):
            artifacts.add(node.value)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DB_CONFIG" for t in node.targets) and isinstance(node.value, ast.Dict):
            for key, value in zip(node.value.keys, node.value.values):
                if isinstance(key, ast.Constant) and key.value == "port" and isinstance(value, ast.Constant) and type(value.value) is int:
                    fixed_port = value.value
        if not isinstance(node, ast.Call): continue
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "environ"
            and isinstance(node.func.value.value, ast.Name) and node.func.value.value.id == "os"
            and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", node.args[0].value)):
            environments.add(node.args[0].value)
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "execute" and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            tables.update(re.findall(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)", node.args[0].value, re.IGNORECASE))
    return {"environment_names": sorted(environments), "literal_sql_tables": sorted(tables),
            "artifact_basenames": sorted(artifacts), "import_modules": sorted(imports), "literal_database_port": fixed_port,
            "scope": "Static AST subset; dynamic SQL, indirect configuration and execution behavior remain unverified"}


def service_markers(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES: raise ValueError("Public source exceeds byte budget")
    text = raw.decode("utf-8-sig")
    return {"environment_names": sorted(set(re.findall(r"\bprocess\.env\.([A-Z][A-Z0-9_]{0,63})\b", text))),
            "fetch_call_marker": bool(re.search(r"\bfetch\s*\(", text)),
            "local_json_import_marker": bool(re.search(r"\bimport\s+\w+\s+from\s+['\"][^'\"]+\.json['\"]", text)),
            "scope": "Text markers only; TypeScript not executed or typechecked"}


def pinned_sources(root, source, *, members=None, max_bytes=MAX_BYTES):
    members = MEMBERS if members is None else members
    if (not isinstance(members, dict) or not 1 <= len(members) <= 5
        or not set(members.values()) <= {*MEMBERS.values(), PUBLIC_RECIPE_MEMBER}
        or type(max_bytes) is not int or not 1 <= max_bytes <= 2 * 1024 * 1024):
        raise ValueError("Only explicitly allowlisted public lifecycle/data blobs may be read")
    pin = source["commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", pin): raise ValueError("Invalid source pin")
    checkout = contained(root, source["checkout"])
    def git(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args], timeout=30,
                                       stderr=subprocess.PIPE, env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
    if git("rev-parse", "HEAD").decode().strip() != pin: raise ValueError("Public source checkout differs from pin")
    raw, identities = {}, {}
    for label, member in members.items():
        entry = git("ls-tree", "-z", pin, "--", member).rstrip(b"\0")
        header, actual = entry.decode().split("\t", 1)
        mode, kind, oid = header.split()
        if actual != member or mode != "100644" or kind != "blob" or not re.fullmatch(r"[0-9a-f]{40}", oid):
            raise ValueError("Public lifecycle source is not one pinned regular blob")
        if not 0 < int(git("cat-file", "-s", oid)) <= max_bytes: raise ValueError("Public lifecycle source exceeds byte budget")
        value = git("cat-file", "blob", oid)
        if len(value) > max_bytes or hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest() != oid:
            raise ValueError("Public lifecycle blob identity changed")
        raw[label] = value
        identities[label] = {"member": member, "git_blob": oid, "sha256": hashlib.sha256(value).hexdigest()}
    return raw, identities


def inspect_lifecycle(root=ROOT):
    source = json.loads((root / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]["toolathlon_gym"]
    raw, identities = pinned_sources(root, source)
    scripts = {name: python_contract(raw[name]) for name in ("preprocess", "evaluation")}
    services = {name: service_markers(raw[name]) for name in ("calendar_service", "recipe_data_loader")}
    return {"schema_version": "second-original-task-lifecycle-design-v1", "task_id": TASK, "source_commit": source["commit"],
            "source_objects": identities, "python_contracts": scripts, "service_markers": services,
            "environment_names_to_bind_per_episode": sorted(set().union(*(set(row["environment_names"]) for row in [*scripts.values(), *services.values()]))),
            "required_isolation_design": "Separate gcal state per episode; preserve evaluator fixed-port requirement using owned per-slot addresses",
            "recipe_determinism_design": "Pin and validate a local recipe snapshot; disable remote fetch in the future project adapter without editing upstream tracked files",
            "runtime_activation": False, "training_admission": False, "reference_solutions_read": False,
            "verified_task_bodies_read": False, "implicit_network_fetch": False, "scripts_executed": False,
            "real_model_calls": 0, "training_updates": 0}


def main():
    report = inspect_lifecycle()
    report["source_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                               ("training/task_lifecycle_design.py", "configs/mainline_sources.lock.json")}
    save(ROOT / "evidence/mainline/next_seed_lifecycle.json", report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__": raise SystemExit(main())
