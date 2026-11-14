"""Real HTTP integration: NeMo -> original MCP/tools -> original task evaluator.

The fixed correct path is a scripted diagnostic, never a sampled model rollout.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import io
import json
from pathlib import Path
import sys
import uuid

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.nemo_http import NeMoHTTP
from integrations.runtime_identity import expected_identity


def money(value):
    return float(value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def main():
    client = NeMoHTTP()
    sid = str(uuid.uuid4())
    episode = {"rollout_id": "nemo-diagnostic-" + uuid.uuid4().hex, "attempt": 0}
    task = {"taskset": "toolathlon_gym:mainline-diagnostic", "task_id": "wc-sales-tax-summary"}
    seed = {"resources_session_id": sid, "episode_id": episode, "task_id": task, "task_data": {}}
    bound = {"resources_session_id": sid, "episode_id": episode}
    verify = {"episode_id": episode, "task_id": task, "verification_input": {"resources_session_id": sid}}
    checks = []
    output = ROOT / "evidence/mainline" / episode["rollout_id"]
    output.mkdir(parents=True, exist_ok=False)

    def check(name, condition):
        checks.append({"name": name, "passed": bool(condition)})
        if not condition:
            raise AssertionError(name)

    def call(server, name, arguments, *, request_id=None):
        return client.post("/tool", {**bound, "request_id": request_id or str(uuid.uuid4()),
                                    "server": server, "name": name, "arguments": arguments})["result"]

    closed = False
    try:
        first = client.post("/seed_session", seed)
        check("idempotent_seed", client.post("/seed_session", seed) == first)
        stranger = NeMoHTTP()
        stranger.post("/tools", bound, expected=409)
        check("cookie_isolation", True)
        schemas = client.post("/tools", bound)
        check("real_original_mcp_schemas", {t["name"] for t in schemas["tools"]} >= {"woo_orders_list", "update_cells"})
        wrong = client.post("/verify", verify)["result"]
        check("measured_business_failure_not_masked", wrong["reward"] == 0 and wrong["mask_sample"] is False)
        orders = []
        for status in ("completed", "processing"):
            for page in range(1, 11):
                data = call("woocommerce", "woo_orders_list", {"status": [status], "perPage": 100, "page": page})
                if not isinstance(data, list): raise RuntimeError("Unexpected original order tool result")
                orders.extend(data)
                if len(data) < 100: break
            else: raise RuntimeError("Diagnostic pagination exceeded budget")
        check("qualifying_orders", bool(orders) and len({o["id"] for o in orders}) == len(orders))
        sales, tax, states = Decimal(0), Decimal(0), {}
        for order in orders:
            sale, amount = Decimal(str(order["total"])), Decimal(str(order["total_tax"]))
            sales += sale
            tax += amount
            state = order.get("billing", {}).get("state")
            if state:
                row = states.setdefault(state, [0, Decimal(0), Decimal(0)])
                row[0] += 1
                row[1] += sale
                row[2] += amount
        overall = [["Metric", "Value"], ["Total Orders", len(orders)], ["Total Sales", money(sales)],
                   ["Total Tax", money(tax)], ["Effective Tax Rate", money(tax / sales * 100) if sales else 0]]
        state_rows = [["State", "Order Count", "Total Sales", "Total Tax"]]
        for state, row in sorted(states.items(), key=lambda p: (-p[1][2], p[0])):
            state_rows.append([state, row[0], money(row[1]), money(row[2])])
        written = client.post("/write_workbook", {**bound, "request_id": str(uuid.uuid4()), "workbook": {
            "sheets": [{"name": "By State", "rows": state_rows}, {"name": "Overall", "rows": overall}]}})
        check("artifact_written_through_nemo", written["result"]["path"] == "Tax_Summary_Report.xlsx" and len(written["result"]["sha256"]) == 64)
        rid = str(uuid.uuid4())
        args = {"title": "Tax Summary for Accounting"}
        created = call("google_sheet", "create_spreadsheet", args, request_id=rid)
        repeated = call("google_sheet", "create_spreadsheet", args, request_id=rid)
        check("mutation_request_replay_returns_original_result", created == repeated)
        sheet_id = created["spreadsheetId"]
        tabs = call("google_sheet", "list_sheets", {"spreadsheet_id": sheet_id})
        call("google_sheet", "rename_sheet", {"spreadsheet": sheet_id, "sheet": tabs[0], "new_name": "Summary"})
        call("google_sheet", "update_cells", {"spreadsheet_id": sheet_id, "sheet": "Summary", "range": "A1:B5", "data": overall})
        call("google_sheet", "get_sheet_data", {"spreadsheet_id": sheet_id, "sheet": "Summary", "range": "A1:B5"})
        correct = client.post("/verify", verify)["result"]
        check("original_and_supplemental_verifiers_pass", correct["reward"] == 1 and correct["mask_sample"] is False and
              correct["evaluation"]["upstream_passed"] and correct["evaluation"]["supplemental_passed"])
        client.post("/close_session", bound)
        closed = True
        check("idempotent_close", client.post("/close_session", bound)["resources_session_id"] == sid)
        client.post("/seed_session", seed, expected=409)
        check("late_seed_cannot_recreate_closed_session", True)
        unknown = client.post("/verify", verify)["result"]
        check("lost_session_is_masked_unknown", unknown["reward"] is None and unknown["mask_sample"] is True)
    finally:
        if not closed:
            try: client.post("/close_session", bound)
            except Exception: pass
        # Retain requests and actual replies, including failures. No private cookie or token.
        (output / "http_journal.json").write_text(json.dumps(client.journal, indent=2) + "\n", encoding="utf-8")
        receipt = {"schema_version": "nemo-toolathlon-diagnostic-v1", "at": datetime.now(timezone.utc).isoformat(),
                   "framework": "nemo_gym", "execution_kind": "scripted_diagnostic", "checks": checks,
                   "passed": len(checks) == 11 and all(c["passed"] for c in checks),
                   "real_model_calls": 0, "training_updates": 0, "admitted_training_samples": 0,
                   "artifact_transport": "nemo_structured_workbook_tool",
                   "source_sha256": {**expected_identity(ROOT), **{name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
                       "integrations/nemo_resources.py", "integrations/toolathlon_bridge.py", "integrations/nemo_http.py",
                       "integrations/resource_ledger.py", "integrations/workbook_tool.py", "scripts/mainline_runtime.py",
                       "adapters/toolathlon_native.py", "scripts/verify_nemo_toolathlon.py", "configs/mainline_sources.lock.json")}},
                   "journal_sha256": hashlib.sha256((output / "http_journal.json").read_bytes()).hexdigest()}
        (output / "result.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"evidence": output.relative_to(ROOT).as_posix(), **receipt}, indent=2))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
