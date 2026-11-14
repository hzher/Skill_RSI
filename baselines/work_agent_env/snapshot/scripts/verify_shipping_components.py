"""Two owned original MCP/PG resources, fixed XLSX and project SQL oracle."""
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
from adapters import shipping_validation as validation
from adapters import meal_plan_artifacts as artifacts
from adapters.shipping_evaluation import evaluate, REFERENCE_MODE, profile_registered
from integrations.shipping_components import ShippingComponents, initialize, snapshot, collect_sources, TASK_ID
from integrations.shipping_audit import CHECKS, SOURCES
from integrations.episode_cluster import reserve_cluster, require_dependents_closed
from scripts.verify_calendar_isolation import shared_snapshot
from scripts.verify_customer_report_components import decode
from workenv.core import ResourceError, contained, save


def main():
    output = ROOT / "evidence/mainline" / ("shipping-components-" + uuid4().hex)
    output.mkdir()
    checks, clusters, measurements, retained, pagination = [], [], [], {}, []
    before, after = shared_snapshot(), None
    def check(name, valid):
        checks.append({"name": name, "passed": bool(valid)})
        if not valid: raise AssertionError(name)
    def measure(i):
        measured = evaluate(ROOT, clusters[i])
        measurements.append({"index": i, "result": measured})
        return measured
    async def run():
        with ExitStack() as owned:
            record = json.loads((ROOT / ".runtime/nemo-vm/isolated-seed.json").read_text())
            seed = contained(ROOT, record["seed_ref"])
            if hashlib.sha256(seed.read_bytes()).hexdigest() != record["sha256"]: raise ResourceError("Owned seed changed")
            for slot in (0, 1):
                cluster = owned.enter_context(reserve_cluster(ROOT, slot=slot, resource_id=str(uuid4())))
                cluster.start(seed)
                clusters.append(cluster)
            async with AsyncExitStack() as services:
                controllers = [await services.enter_async_context(ShippingComponents(ROOT, c).open()) for c in clusters]
                check(CHECKS[0], all(not (c.profile.base / "shipping-task-session/nemo_binding.json").exists() for c in clusters))
                data = [snapshot(c) for c in clusters]
                check(CHECKS[1], all(d["orders"] and not d["cells"] and not d["spreadsheets"] for d in data))
                check(CHECKS[2], all(len(c.schemas) == 9 and len(c.children) == 2 for c in controllers))
                check(CHECKS[3], all(measure(i)["component_measurement_passed"] is False for i in (0, 1)))
                ids, payloads = [], []
                for i, controller in enumerate(controllers):
                    actual, pages = await collect_sources(controller, data[i])
                    pagination.append({"index": i, "pages": pages, "rows": len(actual["orders"]), "inventory_counts": {k:len(v) for k,v in actual.items()}})
                    truth = validation.expected(actual["orders"], actual["shipping_zones"], actual["shipping_zone_locations"], actual["shipping_zone_methods"], source_binding=validation.source_identity())
                    payload = {"Zone Summary": truth["rows"], "Methods": truth["methods"]}
                    payload = json.loads(json.dumps(payload, default=float))
                    payloads.append(payload)
                    artifacts.write_report(ROOT, clusters[i], payload, task="shipping")
                    identifier = decode(await controller.call("google_sheet", "create_spreadsheet", {"title": "Shipping Zone Dashboard"}))["spreadsheetId"]
                    tabs = await controller.call("google_sheet", "list_sheets", {"spreadsheet_id": identifier})
                    names = [c["text"] for c in tabs["content"] if c["type"] == "text"]
                    if tabs.get("isError") or len(names) != 1: raise ResourceError("Original default tab is not unique")
                    decode(await controller.call("google_sheet", "rename_sheet", {"spreadsheet": identifier, "sheet": names[0], "new_name": "Summary"}))
                    grid = [list(validation.HEADERS["Zone Summary"]), *[[r[k] for k in validation.HEADERS["Zone Summary"]] for r in payload["Zone Summary"]]]
                    decode(await controller.call("google_sheet", "update_cells", {"spreadsheet_id": identifier, "sheet": "Summary", "range": "A1:D"+str(len(grid)), "data": grid}))
                    ids.append(identifier)
                check(CHECKS[4], len(pagination) == 2 and all(p["pages"][-1]["rows"] < 10 for p in pagination))
                check(CHECKS[5], all(measure(i)["component_measurement_passed"] is True for i in (0, 1)))
                download = artifacts.read_report(ROOT, clusters[0], task="shipping")
                check(CHECKS[6], hashlib.sha256(base64.b64decode(download["base64"])).hexdigest() == download["sha256"])
                amount = payloads[0]["Zone Summary"][0]["Total_Revenue"]
                async def cloud_amount(value):
                    decode(await controllers[0].call("google_sheet", "update_cells", {"spreadsheet_id": ids[0], "sheet": "Summary", "range": "C2:C2", "data": [[value]]}))
                await cloud_amount(amount + .01)
                bad = measure(0)
                check(CHECKS[7], bad["original_parser_passed"] is True and bad["supplemental_passed"] is False and measure(1)["component_measurement_passed"] is True)
                await cloud_amount(amount)
                wrong = deepcopy(payloads[0]); wrong["Zone Summary"][0]["Total_Revenue"] += .01
                artifacts.write_report(ROOT, clusters[0], wrong, task="shipping")
                bad = measure(0)
                check(CHECKS[8], bad["original_parser_passed"] is True and bad["supplemental_passed"] is False)
                artifacts.write_report(ROOT, clusters[0], payloads[0], task="shipping")
                check(CHECKS[9], measure(0)["component_measurement_passed"] is True)
                wrong = deepcopy(payloads[0])
                pickup = next(r for r in wrong["Methods"] if r["Method"] == "local_pickup")
                pickup["Details"] = pickup["Details"].split("; ")[0]
                artifacts.write_report(ROOT, clusters[0], wrong, task="shipping")
                bad = measure(0)
                check(CHECKS[10], bad["original_parser_passed"] is True and bad["supplemental_passed"] is False)
                artifacts.write_report(ROOT, clusters[0], payloads[0], task="shipping")
                count = len(controllers[0].journal)
                try: await controllers[0].call("woocommerce", "woo_orders_delete", {"id": 1})
                except ValueError: pass
                else: raise AssertionError("Unallowlisted mutation accepted")
                check(CHECKS[11], count == len(controllers[0].journal))
                marker = clusters[0].profile.base / "shipping-initialized.json"
                raw = marker.read_bytes(); foreign = json.loads(raw); foreign["task_id"] = "wc-order-fulfillment-tracker"
                marker.write_text(json.dumps(foreign))
                try:
                    try: evaluate(ROOT, clusters[0])
                    except ResourceError: unknown = True
                    else: unknown = False
                finally: marker.write_bytes(raw)
                check(CHECKS[12], unknown and measure(0)["component_measurement_passed"] is True)
                intent = clusters[0].profile.base / ("shipping-artifact-write-" + uuid4().hex + ".json")
                save(intent, {"state": "prepared", "profile": clusters[0].profile.record()})
                try:
                    try: evaluate(ROOT, clusters[0])
                    except ResourceError: blocked = True
                    else: blocked = False
                finally: save(intent, {"state": "aborted", "profile": clusters[0].profile.record(), "fixture_only": True, "replay_performed": False})
                check(CHECKS[13], blocked and measure(0)["component_measurement_passed"] is True)
            for cluster in clusters: require_dependents_closed(cluster.profile)
            initialize(ROOT, clusters[0])
            check(CHECKS[14], not snapshot(clusters[0])["cells"] and measure(1)["component_measurement_passed"] is True)
            check(CHECKS[15], all(snapshot(c)["readonly_sha256"] == d["readonly_sha256"] for c, d in zip(clusters, data)))
            from integrations.customer_report_components import CustomerReportComponents
            async with CustomerReportComponents(ROOT, clusters[0]).open() as default:
                listed = await default.clients["woocommerce"].list_tools()
                save(clusters[0].profile.base / "shipping-components-run/default-woocommerce-tools.json", [t.name for t in listed.tools])
                check(CHECKS[16], "workenv_shipping_zone_locations_get" not in {t.name for t in listed.tools})

    try:
        asyncio.run(run())
        check(CHECKS[17], all(not (c.profile.data / "postmaster.pid").exists() for c in clusters))
        after = shared_snapshot()
        check(CHECKS[18], before == after)
    finally:
        for cluster in clusters:
            for path in cluster.profile.base.rglob("*"):
                if path.is_file() and "pgdata" not in path.parts and path.suffix in (".json", ".xlsx", ".py", ".txt"):
                    retained[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        save(output / "pagination.json", pagination)
        retained[(output / "pagination.json").relative_to(ROOT).as_posix()] = hashlib.sha256((output / "pagination.json").read_bytes()).hexdigest()
        result = {"schema_version": "shipping-original-components-diagnostic-v1", "at": datetime.now(timezone.utc).isoformat(),
            "passed": [c["name"] for c in checks] == list(CHECKS) and all(c["passed"] for c in checks), "checks": checks,
            "profiles": [c.profile.record() for c in clusters], "measurements": measurements, "reference_mode": REFERENCE_MODE,
            "task_passed": None, "reward": None, "runtime_profile_registered": profile_registered(ROOT), "nemo_task_executed": False,
            "upstream_reference_used": False, "official_benchmark_score_available": False, "training_eligible": False,
            "real_model_calls": 0, "training_updates": 0, "shared_before": before, "shared_after": after,
            "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}, "retained_sha256": retained}
        save(output / "result.json", result)
        print(json.dumps({"result": str(output / "result.json"), "passed": result["passed"], "checks": len(checks)}))


if __name__ == "__main__": main()
