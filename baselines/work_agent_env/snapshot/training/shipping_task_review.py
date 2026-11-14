"""Extra public service identities needed for shipping location registration."""
import hashlib
import json
from pathlib import Path

from training.office_task_review import public_blob, PIN
from training.source_api import pinned_blob
from workenv.core import save

ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "wc-shipping-zone-gsheet"
EXTRA = {
    "local_servers/woocommerce-mcp/src/services/shipping.ts": "3af5f61507dfc4341cec2b1605b9f88a4d1a158a710b39a2ac792a04bab756a2",
    "local_servers/woocommerce-mcp/src/services/base.ts": "0757d36be11a6b6a517d0481305ec848497896272ea39dc268458ffb6feaae14"}
SOURCES = ("training/shipping_task_review.py", "training/office_task_review.py", "training/source_api.py", "configs/mainline_sources.lock.json")


def inspect(root=ROOT):
    source = json.loads((root / "configs/mainline_sources.lock.json").read_text())["sources"]["toolathlon_gym"]
    if source["commit"] != PIN: raise ValueError("Shipping source pin changed")
    identities = {name: public_blob(root, TASK_ID, name)[1] for name in ("task_config.json", "docs/task.md", "preprocess/main.py", "evaluation/main.py")}
    for name, checksum in EXTRA.items():
        raw, identity = pinned_blob(root, source, name)
        if hashlib.sha256(raw).hexdigest() != checksum: raise ValueError("Shipping public service changed")
        identities[name] = identity
    return {"schema_version": "shipping-public-source-review-v1", "passed": True, "task_id": TASK_ID,
        "public_sources": identities, "original_locations_mcp_tool_available": False,
        "project_read_only_registration": "workenv_shipping_zone_locations_get",
        "reference_mode": "project_derived_sql_oracle", "upstream_reference_used": False,
        "official_benchmark_score_available": False, "runtime_profile_registered": False,
        "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCES}}


if __name__ == "__main__":
    report = inspect()
    save(ROOT / "evidence/mainline/shipping_task_review.json", report)
    print(json.dumps({"passed": True, "public_sources": len(report["public_sources"]), "runtime_profile_registered": False}))
