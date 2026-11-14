"""Freeze the original service's public recipe data without starting that service."""
import hashlib
import json
from pathlib import Path
import re

from training.task_lifecycle_design import PUBLIC_RECIPE_MEMBER, pinned_sources
from workenv.core import contained, no_links, save

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 2 * 1024 * 1024
CACHE = ".runtime/native-episodes/seed-cache/howtocook"


def inspect_recipe_data(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("Public recipe snapshot exceeds byte budget")
    rows = json.loads(raw)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4096:
        raise ValueError("Unsupported public recipe dataset")
    ids, categories = set(), set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
            or not 1 <= len(row["id"]) <= 256 or row["id"] in ids
            or not isinstance(row.get("name"), str) or not 1 <= len(row["name"]) <= 1024
            or not isinstance(row.get("category"), str) or len(row["category"]) > 128):
            raise ValueError("Invalid or duplicated recipe identity")
        ids.add(row["id"])
        categories.add(row["category"])
    return {"recipe_count": len(rows), "category_count": len(categories), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "git_blob": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()}


def retain_recipe_data(root, raw):
    root = Path(root).resolve()
    summary = inspect_recipe_data(raw)
    ref = CACHE + "/" + summary["sha256"] + ".json"
    path = contained(root, ref)
    no_links(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream: stream.write(raw)
    except FileExistsError:
        if path.stat().st_size != len(raw) or path.read_bytes() != raw:
            raise ValueError("Existing recipe snapshot differs from its content identity") from None
    return {"asset_ref": ref, **summary}


def verify_snapshot(root, receipt):
    root = Path(root).resolve()
    pin = json.loads((root / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]["toolathlon_gym"]["commit"]
    if (receipt.get("source_commit") != pin or receipt.get("source_object", {}).get("member") != PUBLIC_RECIPE_MEMBER
        or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("sha256", ""))
        or receipt.get("asset_ref") != CACHE + "/" + receipt["sha256"] + ".json"):
        raise ValueError("Recipe snapshot source or cache reference changed")
    path = contained(root, receipt["asset_ref"])
    no_links(path, root)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Recipe snapshot missing or oversized")
    with path.open("rb") as stream: raw = stream.read(MAX_BYTES + 1)
    summary = inspect_recipe_data(raw)
    if (any(receipt.get(k) != value for k, value in summary.items())
        or receipt["source_object"]["sha256"] != summary["sha256"] or receipt["source_object"]["git_blob"] != summary["git_blob"]):
        raise ValueError("Recipe snapshot content identity changed")
    return summary


def main():
    source = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]["toolathlon_gym"]
    values, identities = pinned_sources(ROOT, source, members={"recipe_snapshot": PUBLIC_RECIPE_MEMBER}, max_bytes=MAX_BYTES)
    report = {"schema_version": "howtocook-public-recipe-snapshot-v1", "source": "toolathlon_gym",
              "source_commit": source["commit"], "source_object": identities["recipe_snapshot"],
              **retain_recipe_data(ROOT, values["recipe_snapshot"]), "passed": True,
              "runtime_activation": False, "training_admission": False, "mcp_service_started": False,
              "implicit_network_fetch": False, "reference_solutions_read": False, "verified_task_bodies_read": False,
              "real_model_calls": 0, "training_updates": 0}
    verify_snapshot(ROOT, report)
    report["source_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                              ("training/recipe_snapshot.py", "training/task_lifecycle_design.py", "configs/mainline_sources.lock.json")}
    save(ROOT / "evidence/mainline/howtocook_recipe_snapshot.json", report)
    print(json.dumps({k: report[k] for k in ("passed", "recipe_count", "category_count", "bytes", "sha256", "runtime_activation")}, indent=2))
    return 0


if __name__ == "__main__": raise SystemExit(main())
