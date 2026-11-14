"""Inspect only literal public blobs for the next three office task candidates."""
import ast
import hashlib
import json
from pathlib import Path

from training.source_api import pinned_blob
from workenv.core import save

ROOT = Path(__file__).resolve().parents[1]
PIN = "ed735ba0af71d82688a55f952a6ea11479ae7206"
TASKS = {
    "wc-customer-spending-report": {
        "task_config.json": "6d2bc0fdcfe93bc8b6fd57a55c56f74e52bfd4ecb3612385f15a8c7729f9a60f",
        "docs/task.md": "ab01d97cea5e2c19a1f1817ddfbe6d53c0783b8fd9dc5723483865bc12831180",
        "preprocess/main.py": "e1ccb68f85f71012362c8105eaa7e46e9225d90d0743a810874a318c02dac239",
        "evaluation/main.py": "9f15eb8cbfe82bd4a8c2566647a2c5e4c95d9a30781643e61aaa99f45d864ab8"},
    "wc-order-fulfillment-tracker": {
        "task_config.json": "6d2bc0fdcfe93bc8b6fd57a55c56f74e52bfd4ecb3612385f15a8c7729f9a60f",
        "docs/task.md": "0bba6076b507bd16dfabf3168d95c358a6e34f7f560dd05c2ee2f5e58216cdab",
        "preprocess/main.py": "5a683feb81bb5f38faf40cc4e051b707883f91dda3c46cd81c972a8fbbc657ef",
        "evaluation/main.py": "a0a1ce3899d65f8f291fb406dc970af45910d59ec19e688db084c421e986f245"},
    "wc-shipping-zone-gsheet": {
        "task_config.json": "25f4b8b5635ea7fd3244515c926dcd08220cf07a23578e715df9f9280cb7fded",
        "docs/task.md": "b1b31e6d8763fd752306eb985644fa71473d10b56be9e2757a27fd204500abe3",
        "preprocess/main.py": "600cb38039e3c756eeb4f7050429dabbc358016bcfe2417a44ef50419ac96263",
        "evaluation/main.py": "978a9cf51d3085e5bc9ac1bccc2938676b3e757cc0876ab1b291b7cd9158cc9d"},
}
SERVICES = {
    "local_servers/woocommerce-mcp/src/server.ts": "44c500f56c3a9c7f18397158e6a2140c9e9a4873b757533ee554d38319ef1e7f",
    "local_servers/woocommerce-mcp/src/services/pg-rest-router.ts": "4c5aff8e15d65db597fa49342fbcf5fab55bbda00b3538dad6676e56d77d5dc2",
    "local_servers/mcp-google-sheets/src/mcp_google_sheets/server.py": "1b72f6f2ebcbb87ae85e04f4f294d3b7805c418cf42da90049b603d8913f65d2",
}
SOURCES = ("training/office_task_review.py", "training/source_api.py", "configs/mainline_sources.lock.json")


def public_blob(root, task, member):
    if task not in TASKS: raise ValueError("Unreviewed office task")
    if member in TASKS[task]:
        path, checksum = "tasks/finalpool/" + task + "/" + member, TASKS[task][member]
    elif member in SERVICES:
        path, checksum = member, SERVICES[member]
    else:
        raise ValueError("Only enumerated public task/service files may be read")
    source = json.loads((root / "configs/mainline_sources.lock.json").read_text())["sources"]["toolathlon_gym"]
    if source["commit"] != PIN: raise ValueError("Office source pin changed")
    raw, identity = pinned_blob(root, source, path)
    if hashlib.sha256(raw).hexdigest() != checksum: raise ValueError("Reviewed public blob bytes changed")
    return raw, identity


def script_facts(preprocess, evaluator):
    pre, evaluation = ast.parse(preprocess), ast.parse(evaluator)
    deletes = []
    for node in ast.walk(pre):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                raise ValueError("Preprocess SQL needs explicit review")
            query = node.args[0].value.strip()
            if not query.startswith("DELETE FROM gsheet."): raise ValueError("Unexpected writable SQL scope")
            deletes.append(query.removeprefix("DELETE FROM "))
    if set(deletes) != {"gsheet." + n for n in ("cells", "sheets", "permissions", "spreadsheets", "folders")} or len(deletes) != 5:
        raise ValueError("Office reset inventory changed")
    symbols = {n.id for n in ast.walk(evaluation) if isinstance(n, ast.Name)}
    return {"preprocess_delete_tables": deletes, "preprocess_executed": False,
            "evaluator_mentions_groundtruth_file": "gt_file" in symbols,
            "evaluator_executed": False, "analysis_kind": "AST inspection, not runtime proof"}


def inspect_all(root=ROOT):
    tasks = {}
    for task in TASKS:
        blobs = {name: public_blob(root, task, name) for name in TASKS[task]}
        facts = script_facts(blobs["preprocess/main.py"][0], blobs["evaluation/main.py"][0])
        tasks[task] = {"source_objects": {name: identity for name, (_, identity) in blobs.items()},
                       "facts": facts, "runtime_activation": False, "training_admission": False}
    services = {name: public_blob(root, next(iter(TASKS)), name)[1] for name in SERVICES}
    return {"schema_version": "public-office-task-source-inspection-v1", "source_commit": PIN,
        "tasks": tasks, "shared_service_objects": services, "passed": True,
        "inspection_scope": "15 explicitly allowlisted public blobs; no reference or solution file reads",
        "reference_solutions_read": False, "upstream_groundtruth_read": False, "verified_task_bodies_read": False,
        "real_model_calls": 0, "training_updates": 0, "new_runtime_profiles": 0,
        "source_sha256": {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in SOURCES}}


def main():
    report = inspect_all()
    save(ROOT / "evidence/mainline/office_task_reviews.json", report)
    print(json.dumps({"passed": report["passed"], "tasks_inspected": list(report["tasks"]), "runtime_activation": False}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
