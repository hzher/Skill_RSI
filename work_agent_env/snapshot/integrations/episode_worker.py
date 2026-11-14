"""Private stdio resource worker for a future NeMo multi-episode gateway.

No model or agent loop runs here. One process owns one isolated cluster, and
reuses the original task resource dispatch already exposed through NeMo.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from integrations.episode_cluster import reserve_cluster
from integrations.resource_ledger import ResourceLedger
from integrations.toolathlon_bridge import Resources, Conflict, session_id
from integrations.resource_transport import MAX_REQUEST_FRAME_BYTES, MAX_RESPONSE_FRAME_BYTES, encode_frame
from adapters import toolathlon_native as native
from workenv.process_identity import process_identity


async def serve(resources, expected_sid):
    seen = set()
    response_unknown = False
    try:
        for _ in range(128):
            raw = sys.stdin.buffer.readline(MAX_REQUEST_FRAME_BYTES + 1)
            if not raw: break
            identifier = None
            try:
                if len(raw) > MAX_REQUEST_FRAME_BYTES or not raw.endswith(b"\n"):
                    raise ValueError("Bounded newline-delimited resource frame required")
                frame = json.loads(raw)
                if not isinstance(frame, dict) or set(frame) != {"id", "route", "body"}:
                    raise ValueError("Invalid resource frame")
                identifier = session_id(frame["id"])
                if identifier in seen: raise ValueError("Duplicate resource transport identity")
                seen.add(identifier)
                if not isinstance(frame["body"], dict) or frame["body"].get("resources_session_id") != expected_sid:
                    raise ValueError("Worker is bound to one episode resource identity")
                if frame["route"] not in ("/seed_session", "/close_session", "/tool", "/tools", "/verify", "/write_workbook", "/write_report", "/read_workbook"):
                    raise ValueError("Unknown isolated resource operation")
                if response_unknown and frame["route"] != "/close_session":
                    raise RuntimeError("Previous response outcome is unknown; close the resource")
                result = await resources.dispatch(frame["route"], frame["body"])
                response = {"id": identifier, "status": 200, "result": result}
            except Conflict as exc:
                response = {"id": identifier, "status": 409, "error": str(exc)[:300]}
            except (ValueError, TypeError, KeyError) as exc:
                response = {"id": identifier, "status": 400, "error": str(exc)[:300]}
            except Exception as exc:
                response = {"id": identifier, "status": 503, "error": type(exc).__name__, "mask_sample": True, "reward": None}
            try:
                encoded = encode_frame(response, MAX_RESPONSE_FRAME_BYTES)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                # Dispatch may already have changed business state. Do not report
                # a client error, truncate the result, or permit another operation.
                response_unknown = True
                response = {"id": identifier, "status": 503, "error": "Resource response cannot be transported",
                            "outcome_unknown": True, "mask_sample": True, "reward": None}
                encoded = encode_frame(response, MAX_RESPONSE_FRAME_BYTES)
            sys.stdout.buffer.write(encoded)
            sys.stdout.buffer.flush()
            if identifier is None: break
    finally:
        if resources.active is not None:
            state = resources.sessions[resources.active]
            await resources.dispatch("/close_session", {"resources_session_id": resources.active,
                                                        "episode_id": state["binding"]["episode_id"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument("--slot", type=int, choices=(0, 1), required=True)
    parser.add_argument("--seed-sql", type=Path, required=True)
    parser.add_argument("--task-id", choices=(native.TASK_ID, "howtocook-meal-plan-gcal", "wc-order-analysis-gcal", "wc-order-fulfillment-tracker", "wc-shipping-zone-gsheet", "wc-customer-order-gsheet-email"), default=native.TASK_ID)
    args = parser.parse_args()
    # Validate the source-bound task before reserving a slot or starting PG.
    from integrations.task_routing import TaskRouter
    TaskRouter(native.ROOT, "isolated").authorize({"resources_session_id": args.resource_id,
        "episode_id": {"rollout_id": "private-worker-startup", "attempt": 0},
        "task_id": {"taskset": "toolathlon_gym:private-worker", "task_id": args.task_id}, "task_data": {}})
    vip_seed = None
    if args.task_id == "wc-customer-order-gsheet-email":
        from integrations.vip_seed import validated_seed
        vip_seed, expected_sql = validated_seed(native.ROOT)
        if args.seed_sql.resolve() != expected_sql: raise ValueError('VIP worker SQL differs from bound seed')
    with reserve_cluster(native.ROOT, slot=args.slot, resource_id=args.resource_id) as cluster:
        cluster.start(args.seed_sql)
        if vip_seed is not None:
            from integrations.vip_email_resource import VipEmailResourceAdapter
            ledger = ResourceLedger(cluster.profile.base / "resources-session.json", native.ROOT)
            resources = Resources(ledger, resource_backend="isolated", adapter=VipEmailResourceAdapter(native.ROOT, cluster, runtime_seed=vip_seed))
            print(json.dumps({"kind": "isolated-resource-worker-ready", "pid": os.getpid(), "task_id": args.task_id,
                "process_identity": process_identity(os.getpid()), "profile": cluster.profile.record()}), flush=True)
            asyncio.run(serve(resources, args.resource_id))
            return 0
        if args.task_id == "wc-shipping-zone-gsheet":
            from integrations.shipping_resource import ShippingResourceAdapter
            ledger = ResourceLedger(cluster.profile.base / "resources-session.json", native.ROOT)
            resources = Resources(ledger, resource_backend="isolated", adapter=ShippingResourceAdapter(native.ROOT, cluster))
            print(json.dumps({"kind": "isolated-resource-worker-ready", "pid": os.getpid(), "task_id": args.task_id,
                "process_identity": process_identity(os.getpid()), "profile": cluster.profile.record()}), flush=True)
            asyncio.run(serve(resources, args.resource_id))
            return 0
        if args.task_id == "wc-order-fulfillment-tracker":
            from integrations.fulfillment_resource import FulfillmentResourceAdapter
            ledger = ResourceLedger(cluster.profile.base / "resources-session.json", native.ROOT)
            resources = Resources(ledger, resource_backend="isolated", adapter=FulfillmentResourceAdapter(native.ROOT, cluster))
            print(json.dumps({"kind": "isolated-resource-worker-ready", "pid": os.getpid(), "task_id": args.task_id,
                "process_identity": process_identity(os.getpid()), "profile": cluster.profile.record()}), flush=True)
            asyncio.run(serve(resources, args.resource_id))
            return 0
        if args.task_id == "wc-order-analysis-gcal":
            from integrations.order_analysis_resource import OrderAnalysisResourceAdapter
            ledger = ResourceLedger(cluster.profile.base / "resources-session.json", native.ROOT)
            resources = Resources(ledger, resource_backend="isolated", adapter=OrderAnalysisResourceAdapter(native.ROOT, cluster))
            print(json.dumps({"kind": "isolated-resource-worker-ready", "pid": os.getpid(), "task_id": args.task_id,
                "process_identity": process_identity(os.getpid()), "profile": cluster.profile.record()}), flush=True)
            asyncio.run(serve(resources, args.resource_id))
            return 0
        if args.task_id == "howtocook-meal-plan-gcal":
            from integrations.meal_plan_resource import MealPlanResourceAdapter
            ledger = ResourceLedger(cluster.profile.base / "resources-session.json", native.ROOT)
            resources = Resources(ledger, resource_backend="isolated", adapter=MealPlanResourceAdapter(native.ROOT, cluster))
            print(json.dumps({"kind": "isolated-resource-worker-ready", "pid": os.getpid(), "task_id": args.task_id,
                "process_identity": process_identity(os.getpid()), "profile": cluster.profile.record()}), flush=True)
            asyncio.run(serve(resources, args.resource_id))
            return 0
        with native.episode_resource(cluster.profile):
            ledger = ResourceLedger(cluster.profile.base / "resources-session.json", native.ROOT)
            resources = Resources(ledger, resource_backend="isolated")
            print(json.dumps({"kind": "isolated-resource-worker-ready", "pid": os.getpid(),
                              "process_identity": process_identity(os.getpid()), "profile": cluster.profile.record()}), flush=True)
            asyncio.run(serve(resources, args.resource_id))
    return 0


if __name__ == "__main__": raise SystemExit(main())
