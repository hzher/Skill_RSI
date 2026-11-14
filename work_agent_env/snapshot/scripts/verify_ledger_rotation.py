"""Actual NeMo anti-replay and new-session checks across one offline rotation."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.request
import uuid

from mainline_runtime import ROOT, VM, bridge_request, start_bridge, stop_bridge
sys.path.insert(0, str(ROOT))
from integrations.ledger_rotation import rotation_plan, rotate_ledger, locations, recover_rotation
from integrations.resource_ledger import ResourceLedger
from integrations.nemo_http import NeMoHTTP
from integrations.runtime_identity import expected_identity
from integrations.runtime_identity import classify
from integrations.rotation_audit import rotation_backend, fresh_tools, LEDGERS
from workenv.core import save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", choices=("synthetic", "isolated"), default="synthetic")
    args = parser.parse_args()
    filename, taskset, task_id = rotation_backend(args.ledger)
    prefix = "/" + args.ledger
    client = NeMoHTTP()
    sid = str(uuid.uuid4())
    episode = {"rollout_id": "ledger-rotation-" + uuid.uuid4().hex, "attempt": 0}
    task = {"taskset": taskset, "task_id": task_id}
    bound = {"resources_session_id": sid, "episode_id": episode}
    seed = {**bound, "task_id": task, "task_data": {}}
    verify = {"episode_id": episode, "task_id": task, "verification_input": {"resources_session_id": sid}}
    output = ROOT / "evidence/mainline" / episode["rollout_id"]
    output.mkdir()
    path = VM / filename
    checks, result = [], None
    other_before = {name: (VM / name).read_bytes() for name in LEDGERS if name != filename}
    other_hashes = {}
    for name, raw in other_before.items():
        (output / ("before-" + name)).write_bytes(raw)
    def check(name, valid):
        checks.append({"name": name, "passed": bool(valid)})
        if not valid: raise AssertionError(name)
    def provenance():
        with urllib.request.urlopen("http://127.0.0.1:18700/provenance", timeout=5) as response:
            return json.load(response)
    def post(route, body, expected=200): return client.post(prefix + route, body, expected=expected)
    try:
        status = bridge_request("/status")
        pin = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text())["sources"]["nemo_gym"]["commit"]
        owned = classify(provenance(), status, expected_identity(ROOT), pin)["ready"]
        check("owned_bridges_idle", owned and status["active_session"] is None and not status.get("synthetic", {}).get("active_sessions")
              and not status.get("isolated", {}).get("active_sessions"))
        try:
            post("/seed_session", seed)
        finally:
            post("/close_session", bound)
        before = path.read_bytes()
        plan = rotation_plan(path, ROOT)
        old_rows = json.loads(before)["sessions"]
        check("all_current_identities_closed", all(row["state"] == "closed" for row in old_rows.values()))
        check("plan_is_read_only", path.read_bytes() == before and plan["dry_run"] is True)
        pid = provenance()["pid"]
        stop_bridge()
        try:
            check("owned_bridge_stopped", not (ROOT / ".runtime/native_task.lock").exists())
            result = rotate_ledger(path, ROOT, expected_sha256=plan["expected_sha256"])
        except Exception:
            _, _, pending = locations(path, ROOT)
            if pending.exists() and json.loads(pending.read_bytes()).get("state") == "pending":
                recover_rotation(path, ROOT)
            raise
        finally:
            start_bridge(recover=True)
        ledger = ResourceLedger(path, ROOT)
        check("closed_identities_retired_in_new_generation", set(old_rows) <= set(ledger.retired) and ledger.rows == {})
        check("archive_bytes_unchanged", (ROOT / result["archive_ref"]).read_bytes() == before)
        check("same_nemo_process", provenance()["pid"] == pid)
        post("/seed_session", seed, expected=409)
        check("old_sid_cannot_reseed", True)
        post("/close_session", bound)
        check("old_close_remains_idempotent", True)
        unknown = post("/verify", verify)["result"]
        check("old_closed_session_remains_unknown", unknown["reward"] is None and unknown["mask_sample"] is True)
        changed = {**bound, "episode_id": {**episode, "attempt": 1}}
        post("/close_session", changed, expected=409)
        check("old_episode_binding_enforced", True)
        new_sid = str(uuid.uuid4())
        new_bound = {**bound, "resources_session_id": new_sid}
        try:
            post("/seed_session", {**seed, "resources_session_id": new_sid})
            tools = post("/tools", new_bound)
            works = fresh_tools(args.ledger, tools)
            if args.ledger == "isolated":
                orders = post("/tool", {**new_bound, "request_id": str(uuid.uuid4()), "server": "woocommerce",
                    "name": "woo_orders_list", "arguments": {"perPage": 1, "page": 1}})["result"]
                works = works and isinstance(orders, list) and bool(orders)
            check("fresh_session_works", works)
        finally:
            post("/close_session", new_bound)
        check("new_generation_contains_only_fresh_identity", set(ResourceLedger(path, ROOT).rows) == {new_sid})
        check("original_ledger_unchanged", all((VM / name).read_bytes() == raw for name, raw in other_before.items()))
    finally:
        for name, before in other_before.items():
            after = (VM / name).read_bytes()
            (output / ("after-" + name)).write_bytes(after)
            other_hashes[name] = {"before_sha256": hashlib.sha256(before).hexdigest(), "after_sha256": hashlib.sha256(after).hexdigest()}
        raw = (json.dumps(client.journal, indent=2) + "\n").encode()
        (output / "http_journal.json").write_bytes(raw)
        names = ("scripts/verify_ledger_rotation.py", "scripts/rotate_resources_ledger.py", "integrations/resource_ledger.py",
                 "integrations/ledger_rotation.py", "integrations/synthetic_backend.py", "integrations/rotation_audit.py",
                 "integrations/nemo_http.py", "integrations/runtime_identity.py", "scripts/mainline_runtime.py")
        receipt = {"schema_version": "nemo-ledger-rotation-diagnostic-v2", "at": datetime.now(timezone.utc).isoformat(),
            "passed": len(checks) == 14 and all(c["passed"] for c in checks), "checks": checks, "rotation": result,
            "ledger": args.ledger, "other_ledgers": other_hashes, "business_task_executed": False,
            "official_benchmark_score_available": False,
            "execution_kind": "scripted_resource_diagnostic", "real_model_calls": 0, "training_updates": 0,
            "journal_sha256": hashlib.sha256(raw).hexdigest(),
            "source_sha256": {**expected_identity(ROOT), **{n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in names}}}
        save(output / "result.json", receipt)
        print(json.dumps({"passed": receipt["passed"], "checks": len(checks), "receipt": str((output / "result.json").relative_to(ROOT))}))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
