"""Original MCP/SQL/XLSX customer components; no NeMo activation or official score."""
import asyncio
import base64
from contextlib import AsyncExitStack, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adapters import customer_spending_validation as validation
from adapters import meal_plan_artifacts as artifacts
from adapters.customer_report_evaluation import evaluate
from integrations.customer_report_components import CustomerReportComponents, initialize, snapshot, TASK_ID
from integrations.customer_report_audit import CHECKS, SOURCES
from integrations.episode_cluster import reserve_cluster, require_dependents_closed
from scripts.verify_calendar_isolation import shared_snapshot
from workenv.core import ResourceError, contained, save


def decode(result):
    if result.get("isError"): raise ResourceError("Original customer diagnostic tool returned an error")
    return json.loads("".join(c["text"] for c in result["content"] if c["type"] == "text"))


def main():
    output = ROOT / "evidence/mainline" / ("customer-components-" + uuid4().hex)
    output.mkdir()
    checks, clusters, measurements, retained, pagination, discrepancies, cleanup = [], [], [], {}, [], [], []
    before, after = shared_snapshot(), None
    def check(name, valid):
        checks.append({"name": name, "passed": bool(valid)})
        if not valid: raise AssertionError(name)
    def measure(i):
        result = evaluate(ROOT, clusters[i], basis=validation.BASES[0])
        measurements.append({"index": i, "result": result})
        return result
    async def run():
        with ExitStack() as owned:
            seed_record = json.loads((ROOT / ".runtime/nemo-vm/isolated-seed.json").read_text())
            seed = contained(ROOT, seed_record["seed_ref"])
            if hashlib.sha256(seed.read_bytes()).hexdigest() != seed_record["sha256"]: raise ResourceError("Owned seed changed")
            for slot in (0, 1):
                cluster = owned.enter_context(reserve_cluster(ROOT, slot=slot, resource_id=str(uuid4())))
                cluster.start(seed)
                clusters.append(cluster)
            async with AsyncExitStack() as services:
                controllers = [await services.enter_async_context(CustomerReportComponents(ROOT, c).open()) for c in clusters]
                check(CHECKS[0], TASK_ID not in {p["task_id"] for p in json.loads((ROOT / "configs/task_adapter_profiles.json").read_text())["profiles"].values()})
                data = [snapshot(c) for c in clusters]
                check(CHECKS[1], all(not d["cells"] and not d["spreadsheets"] and d["customers"] and d["orders"] for d in data))
                check(CHECKS[2], all(len(c.schemas) == 7 and len(c.children) == 2 for c in controllers))
                check(CHECKS[3], all(measure(i)["component_measurement_passed"] is False for i in (0, 1)))
                spreadsheet_ids, payloads = [], []
                for i, controller in enumerate(controllers):
                    original = {}
                    for tool, table in (("woo_customers_list", "customers"), ("woo_orders_list", "orders")):
                        rows, pages = [], []
                        for page in range(1, 65):
                            values = decode(await controller.call("woocommerce", tool, {"page": page, "perPage": 10}))
                            if not isinstance(values, list): raise ResourceError("Original page is not an array")
                            pages.append({"page": page, "rows": len(values)})
                            rows.extend(values)
                            if len(values) < 10: break
                        else: raise ResourceError("Customer pagination budget exceeded")
                        truth = {r["id"]: r for r in data[i][table]}
                        if len({r["id"] for r in rows}) != len(rows) or {r["id"] for r in rows} != set(truth):
                            raise AssertionError("Original pagination incomplete or duplicated")
                        fields = ("orders_count", "total_spent", "first_name", "last_name", "email") if table == "customers" else ("customer_id", "total")
                        for row in rows:
                            for field in fields:
                                left, right = row[field], truth[row["id"]][field]
                                if field in ("orders_count", "total_spent", "customer_id", "total"):
                                    if Decimal(str(left)) != Decimal(str(right)): raise AssertionError("Original numeric source differs")
                                elif left != right: raise AssertionError("Original customer identity differs")
                        pagination.append({"index": i, "tool": tool, "pages": pages, "rows": len(rows)})
                        original[table] = rows
                    truth = validation.expected(original["customers"], original["orders"], basis=validation.BASES[0])
                    discrepancies.append(truth["comparison"]["different_customers"])
                    payload = {"Customer Spending": truth["rows"], "Summary": [{"Metric": k, "Value": v} for k, v in truth["summary"].items()]}
                    payload = json.loads(json.dumps(payload, default=float))
                    payloads.append(payload)
                    artifacts.write_report(ROOT, clusters[i], payload, task="customer_report")
                    created = decode(await controller.call("google_sheet", "create_spreadsheet", {"title": "Customer Analysis"}))
                    identifier = created["spreadsheetId"]
                    tabs = await controller.call("google_sheet", "list_sheets", {"spreadsheet_id": identifier})
                    names = [c["text"] for c in tabs["content"] if c["type"] == "text"]
                    if tabs.get("isError") or len(names) != 1: raise ResourceError("Original default tab is not unique")
                    decode(await controller.call("google_sheet", "rename_sheet", {"spreadsheet": identifier, "sheet": names[0], "new_name": "Spending"}))
                    grid = [list(validation.HEADERS["Customer Spending"]), *[list(row.values()) for row in payload["Customer Spending"]]]
                    decode(await controller.call("google_sheet", "update_cells", {"spreadsheet_id": identifier, "sheet": "Spending", "range": "A1:F"+str(len(grid)), "data": grid}))
                    spreadsheet_ids.append(identifier)
                check(CHECKS[4], len(pagination) == 4 and all(p["pages"][-1]["rows"] < 10 for p in pagination))
                check(CHECKS[5], all(discrepancies) and all(measure(i)["component_measurement_passed"] is True for i in (0, 1)))
                downloaded = artifacts.read_report(ROOT, clusters[0], task="customer_report")
                check(CHECKS[6], hashlib.sha256(base64.b64decode(downloaded["base64"])).hexdigest() == downloaded["sha256"])
                original_total = payloads[0]["Customer Spending"][0]["Total_Spent"]
                decode(await controllers[0].call("google_sheet", "update_cells", {"spreadsheet_id": spreadsheet_ids[0], "sheet": "Spending", "range": "E2:E2", "data": [[original_total + .01]]}))
                bad = measure(0)
                check(CHECKS[7], bad["original_parser_passed"] is True and bad["supplemental_passed"] is False and measure(1)["component_measurement_passed"] is True)
                decode(await controllers[0].call("google_sheet", "update_cells", {"spreadsheet_id": spreadsheet_ids[0], "sheet": "Spending", "range": "E2:E2", "data": [[original_total]]}))
                wrong = deepcopy(payloads[0]); wrong["Customer Spending"][0]["Total_Spent"] += .01
                artifacts.write_report(ROOT, clusters[0], wrong, task="customer_report")
                bad = measure(0)
                check(CHECKS[8], bad["original_parser_passed"] is True and bad["supplemental_passed"] is False)
                artifacts.write_report(ROOT, clusters[0], payloads[0], task="customer_report")
                check(CHECKS[9], measure(0)["component_measurement_passed"] is True)
                calls = len(controllers[0].journal)
                try: await controllers[0].call("woocommerce", "woo_customers_delete", {"id": 1})
                except ValueError: pass
                else: raise AssertionError("Unallowlisted mutation accepted")
                check(CHECKS[10], calls == len(controllers[0].journal))
                marker = clusters[0].profile.base / "customer-report-initialized.json"
                raw = marker.read_bytes(); bad_marker = json.loads(raw); bad_marker["profile"] = {}
                marker.write_text(json.dumps(bad_marker))
                try:
                    try: evaluate(ROOT, clusters[0], basis=validation.BASES[0])
                    except ResourceError: unknown = True
                    else: unknown = False
                finally: marker.write_bytes(raw)
                check(CHECKS[11], unknown and measure(0)["component_measurement_passed"] is True)
            for cluster in clusters: require_dependents_closed(cluster.profile)
            initialize(ROOT, clusters[0])
            check(CHECKS[12], not snapshot(clusters[0])["cells"] and measure(1)["component_measurement_passed"] is True)
            check(CHECKS[13], all(snapshot(c)["readonly_sha256"] == d["readonly_sha256"] for c, d in zip(clusters, data)))
    try:
        asyncio.run(run())
        check(CHECKS[14], all(not (c.profile.data / "postmaster.pid").exists() for c in clusters))
        after = shared_snapshot()
        check(CHECKS[15], before == after)
    finally:
        for cluster in clusters:
            for path in cluster.profile.base.rglob("*"):
                if path.is_file() and "pgdata" not in path.parts and path.suffix in (".json", ".xlsx", ".py", ".txt"):
                    retained[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        save(output / "pagination.json", pagination)
        retained[(output / "pagination.json").relative_to(ROOT).as_posix()] = hashlib.sha256((output / "pagination.json").read_bytes()).hexdigest()
        result = {"schema_version": "customer-report-original-components-diagnostic-v1", "at": datetime.now(timezone.utc).isoformat(),
            "passed": [c["name"] for c in checks] == list(CHECKS) and all(c["passed"] for c in checks), "checks": checks,
            "profiles": [c.profile.record() for c in clusters], "measurements": measurements, "source_policy_discrepancies": discrepancies,
            "retained_sha256": retained, "shared_before": before, "shared_after": after,
            "reference_mode": "project_derived_oracle", "spending_basis_approved": False, "runtime_profile_registered": False,
            "nemo_task_executed": False, "upstream_reference_used": False, "official_benchmark_score_available": False,
            "task_passed": None, "reward": None, "training_eligible": False, "real_model_calls": 0, "training_updates": 0,
            "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}}
        save(output / "result.json", result)
        print(json.dumps({"passed": result["passed"], "checks": len(checks), "receipt": (output / "result.json").relative_to(ROOT).as_posix()}))
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
