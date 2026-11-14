"""Compare loaded-process adapter identities with the intended source snapshot."""
import hashlib
from pathlib import Path

BRIDGE_FILES = ("integrations/toolathlon_bridge.py", "integrations/resource_ledger.py", "integrations/workbook_tool.py",
                "integrations/synthetic_backend.py", "integrations/episode_cluster.py", "adapters/toolathlon_native.py",
                "adapters/woocommerce_native.mjs", "integrations/ledger_rotation.py", "integrations/isolated_gateway.py",
                "integrations/episode_worker.py", "integrations/episode_worker_client.py", "integrations/resource_transport.py", "integrations/task_routing.py",
                "integrations/task_profiles.py", "configs/task_adapter_profiles.json",
                "configs/mainline_sources.lock.json", "training/toolathlon_seed_capabilities.json",
                "integrations/meal_plan_resource.py", "integrations/meal_plan_services.py",
                "integrations/calendar_component.py", "integrations/howtocook_component.py",
                "adapters/meal_plan_artifacts.py", "adapters/meal_plan_evaluation.py", "adapters/meal_plan_validation.py")
BRIDGE_FILES += ("workenv/process_identity.py", "workenv/postgres_binary.py")
BRIDGE_FILES += ("integrations/order_analysis_components.py", "integrations/order_analysis_resource.py",
                 "adapters/order_analysis_evaluation.py", "adapters/order_analysis_validation.py")
BRIDGE_FILES += ("adapters/customer_spending_validation.py", "adapters/office_workbook.py")
BRIDGE_FILES += ("integrations/fulfillment_resource.py", "integrations/fulfillment_components.py", "integrations/customer_report_components.py",
                 "adapters/fulfillment_evaluation.py", "adapters/fulfillment_validation.py", "adapters/customer_report_evaluation.py",
                 "adapters/google_sheets_native.py", "training/office_task_review.py")
BRIDGE_FILES += ("integrations/office_report_resource.py", "integrations/shipping_resource.py", "integrations/shipping_components.py",
                 "adapters/shipping_evaluation.py", "adapters/shipping_validation.py", "training/shipping_task_review.py")
NEMO_FILE = "integrations/nemo_resources.py"
BRIDGE_FILES += ("integrations/vip_seed.py", "integrations/vip_email_resource.py", "integrations/vip_email_components.py",
                 "integrations/email_components.py", "integrations/email_build.py", "adapters/email_pg_native.py",
                 "adapters/vip_email_evaluation.py", "adapters/vip_email_validation.py", "training/email_task_review.py",
                 "configs/email.requirements.in", "configs/email.requirements.lock.txt")
BRIDGE_FILES += ("integrations/email_task_identity.py", "integrations/inventory_data.py",
                 "training/inventory_task_review.py", "training/source_api.py",
                 "adapters/inventory_alert_validation.py", "adapters/inventory_alert_artifacts.py", "workenv/core.py",
                 "integrations/mainline_audit.py", "workenv/leases.py")
NEMO_EXTENSIONS = ("integrations/nemo_synthetic_resources.py",)


def expected_identity(root: Path):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in (*BRIDGE_FILES, NEMO_FILE, *NEMO_EXTENSIONS)}


def classify(provenance, bridge, expected, nemo_pin):
    framework = bool(provenance and provenance.get("framework") == "nemo_gym" and provenance.get("source_commit") == nemo_pin)
    nemo_matches = framework and provenance.get("loaded_adapter_sha256") == expected[NEMO_FILE] and all(
        provenance.get("loaded_extension_sha256", {}).get(name) == expected[name] for name in NEMO_EXTENSIONS)
    bridge_matches = bool(bridge and bridge.get("resource_backend") == "original_toolathlon" and
                          all(bridge.get("loaded_source_sha256", {}).get(name) == expected[name] for name in BRIDGE_FILES))
    return {"ready": bool(nemo_matches and bridge_matches), "framework_matches": framework,
            "loaded_nemo_source_matches": bool(nemo_matches), "loaded_bridge_source_matches": bridge_matches,
            "active_session": (bridge.get("active_session") or next(iter(bridge.get("synthetic", {}).get("active_sessions", [])), None)
                               or next(iter(bridge.get("isolated", {}).get("active_sessions", [])), None)) if bridge else None}
