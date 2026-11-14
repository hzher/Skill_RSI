"""Read nine fixed public blobs for a later office task; execute no service."""
import ast
import hashlib
import json
from pathlib import Path

from training.source_api import pinned_blob
from training.task_lifecycle_design import python_contract
from integrations.mainline_audit import read_json, source_binding
from workenv.core import save

ROOT = Path(__file__).resolve().parents[1]
PIN = "ed735ba0af71d82688a55f952a6ea11479ae7206"
TASK_ID = "wc-customer-order-gsheet-email"
BASE = "tasks/finalpool/" + TASK_ID + "/"
PUBLIC_SHA256 = {
    BASE + "task_config.json": "47720d3b2b70eacb1237c5b8dc62088dd8a6d6f6dbf25828e1c381e3c9cc7f02",
    BASE + "docs/task.md": "4686e211f7ca97283ed387d8f7cd28dd1d88d467929af7e5548ff195b90cf8e4",
    BASE + "preprocess/main.py": "c2837069e98983e362b0f39d1bcb6d21ce485d84f12f90411073300b0e7da7d7",
    BASE + "evaluation/main.py": "6a00b47b654e37a62a313e6b4e0de1b3cc167f8c7570146cb71536784ddc1cfa",
    "local_servers/emails-mcp/pyproject.toml": "d3d7aca2f6be831578f5b6dcc6bcf27ded9840ceb2dfb3d51accd6a720442de8",
    "local_servers/emails-mcp/src/emails_mcp/server.py": "44a322cdc925b95b4719969d58073bfd8f0c5797994a53a51615c933265bc301",
    "local_servers/emails-mcp/src/emails_mcp/config/settings.py": "2f710722e9d3429b45dcae341ebf2cdc685034a5efdf8859c7f533da394650d0",
    "local_servers/emails-mcp/src/emails_mcp/backends/pg_backend.py": "0e05dbcbf51994a8d4639ed0c135d2ed21c2e2c717c67aec233a1d4713db90ce",
    "local_servers/emails-mcp/src/emails_mcp/tools/email_tools.py": "0497b48797c70aaf7fce8ddb5ff28a1bd51a7b96d53f78c56effef7e45ad09dc",
}
SERVICES = ["emails", "filesystem", "google_sheet", "woocommerce"]
SOURCES = ("training/email_task_review.py", "training/source_api.py", "training/task_lifecycle_design.py",
           "configs/mainline_sources.lock.json", "training/toolathlon_seed_capabilities.json")


def public_blob(root, member):
    if member not in PUBLIC_SHA256: raise ValueError("Unlisted public email task/service member")
    source = json.loads((root / SOURCES[3]).read_text())["sources"]["toolathlon_gym"]
    if source["commit"] != PIN: raise ValueError("Email task source pin changed")
    raw, identity = pinned_blob(root, source, member)
    if not 0 < len(raw) <= 256 * 1024 or hashlib.sha256(raw).hexdigest() != PUBLIC_SHA256[member]:
        raise ValueError("Reviewed email public source bytes changed")
    return raw, identity


def inspect_task(root=ROOT):
    root = Path(root)
    blobs = {name: public_blob(root, name) for name in PUBLIC_SHA256}
    config = json.loads(blobs[BASE + "task_config.json"][0])
    metadata = json.loads((root / SOURCES[4]).read_text())
    row = metadata["tasks"][TASK_ID]
    if (metadata.get("source") != "toolathlon_gym" or metadata.get("commit") != PIN
        or row.get("configuration_status") != "inspected"
        or row.get("config_sha256") != hashlib.sha256(blobs[BASE + "task_config.json"][0]).hexdigest()
        or sorted(config["needed_mcp_servers"]) != SERVICES or row["needed_mcp_servers"] != SERVICES):
        raise ValueError("Email task configuration metadata differs")
    scripts = {name: python_contract(blobs[BASE + name + "/main.py"][0]) for name in ("preprocess", "evaluation")}
    expected_tables = {"email." + n for n in ("messages", "attachments", "drafts", "sent_log")}
    expected_tables |= {"gsheet." + n for n in ("cells", "sheets", "permissions", "spreadsheets")}
    if set(scripts["preprocess"]["literal_sql_tables"]) != expected_tables or "wc.customers" not in scripts["evaluation"]["literal_sql_tables"]:
        raise ValueError("Email public lifecycle table inventory differs")
    backend = "local_servers/emails-mcp/src/emails_mcp/backends/pg_backend.py"
    tree = ast.parse(blobs[backend][0])
    classes = sorted(n.name for n in tree.body if isinstance(n, ast.ClassDef))
    if "PgSMTPBackend" not in classes: raise ValueError("Email PG replacement backend is absent")
    return {"schema_version": "email-public-task-source-review-v1", "passed": True, "task_id": TASK_ID,
        "source_commit": PIN, "objects": {n: identity for n, (_, identity) in blobs.items()},
        "script_facts": scripts, "pg_backend_facts": python_contract(blobs[backend][0]), "pg_backend_classes": classes,
        "inspection_scope": "Nine literal public files; AST subset, no task/evaluator/service execution",
        "runtime_requirements": ["explicit_owned_pg_backend_without_imap_smtp_fallback", "isolated_wc_gsheet_email_seed",
            "dual_pg_environment_binding", "customer_basis_ties_and_exact_cloud_content_review",
            "local_email_record_idempotence_and_exact_recipient_checks", "actual_nemo_and_lifecycle_acceptance"],
        "runtime_activation": False, "new_runtime_profiles": 0, "task_passed": None, "reward": None,
        "training_admission": False, "scripts_executed": False, "email_service_executed": False,
        "external_messages_sent": 0, "real_model_calls": 0, "training_updates": 0,
        "reference_solutions_read": False, "verified_task_bodies_read": False,
        "source_sha256": {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in SOURCES}}


def audit_review(root, path):
    value = read_json(path, root)
    if value != inspect_task(root): raise ValueError("Email public review summary differs from pinned source")
    source_binding(root, value["source_sha256"])
    return {"public_blobs_verified": len(value["objects"]), "runtime_activation": False,
            "external_messages_sent": 0, "reference_solutions_read": False}


def main():
    report = inspect_task()
    save(ROOT / "evidence/mainline/email_task_review.json", report)
    print(json.dumps({"passed": True, "task_id": TASK_ID, "public_blobs_verified": len(report["objects"]),
                      "runtime_activation": False, "external_messages_sent": 0}))


if __name__ == "__main__": main()
