"""Source-bound adapter contracts; resource allocation validates these profiles.

Resolving a profile alone neither allocates a resource nor admits training data.
"""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from workenv.core import no_links, save

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = "configs/task_adapter_profiles.json"
TAX_IMPLEMENTATION_FILES = ("adapters/toolathlon_native.py", "adapters/woocommerce_native.mjs", "integrations/toolathlon_bridge.py")
MEAL_IMPLEMENTATION_FILES = ("integrations/meal_plan_services.py", "integrations/meal_plan_resource.py",
    "integrations/episode_worker.py", "integrations/calendar_component.py", "integrations/howtocook_component.py",
    "adapters/meal_plan_artifacts.py", "adapters/meal_plan_evaluation.py", "adapters/meal_plan_validation.py")
ORDER_IMPLEMENTATION_FILES = ("integrations/order_analysis_resource.py", "integrations/order_analysis_components.py",
    "adapters/order_analysis_evaluation.py", "adapters/order_analysis_validation.py", "adapters/meal_plan_artifacts.py",
    "adapters/woocommerce_native.mjs", "integrations/calendar_component.py", "integrations/episode_worker.py")
FULFILLMENT_IMPLEMENTATION_FILES = ("integrations/office_report_resource.py", "integrations/fulfillment_resource.py", "integrations/fulfillment_components.py",
    "integrations/customer_report_components.py", "adapters/fulfillment_evaluation.py", "adapters/fulfillment_validation.py",
    "adapters/customer_report_evaluation.py", "adapters/customer_spending_validation.py", "adapters/office_workbook.py",
    "adapters/meal_plan_artifacts.py", "adapters/woocommerce_native.mjs", "adapters/google_sheets_native.py",
    "integrations/order_analysis_resource.py", "integrations/episode_worker.py", "training/office_task_review.py")
SHIPPING_IMPLEMENTATION_FILES = ("integrations/shipping_resource.py", "integrations/shipping_components.py",
    "adapters/shipping_evaluation.py", "adapters/shipping_validation.py", "training/shipping_task_review.py",
    *FULFILLMENT_IMPLEMENTATION_FILES)
VIP_IMPLEMENTATION_FILES = ("integrations/vip_email_resource.py", "integrations/vip_seed.py", "integrations/vip_email_components.py",
    "integrations/email_components.py", "integrations/email_build.py", "adapters/email_pg_native.py",
    "adapters/vip_email_evaluation.py", "adapters/vip_email_validation.py", "training/email_task_review.py",
    "configs/email.requirements.in", "configs/email.requirements.lock.txt", "integrations/episode_worker.py",
    "integrations/customer_report_components.py", "integrations/order_analysis_resource.py", "adapters/google_sheets_native.py",
    "adapters/woocommerce_native.mjs", "adapters/customer_spending_validation.py", "adapters/customer_report_evaluation.py",
    "integrations/toolathlon_bridge.py", "integrations/email_task_identity.py", "integrations/inventory_data.py",
    "training/inventory_task_review.py", "training/source_api.py", "adapters/inventory_alert_validation.py",
    "adapters/inventory_alert_artifacts.py", "integrations/episode_cluster.py", "workenv/core.py", "workenv/process_identity.py",
    "integrations/mainline_audit.py", "workenv/leases.py")
IMPLEMENTATION_FILES = (*TAX_IMPLEMENTATION_FILES, *MEAL_IMPLEMENTATION_FILES, *ORDER_IMPLEMENTATION_FILES, *FULFILLMENT_IMPLEMENTATION_FILES, *SHIPPING_IMPLEMENTATION_FILES, *VIP_IMPLEMENTATION_FILES)


def read_owned(root, name):
    path = root / name
    no_links(path, root)
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Missing or oversized task-profile input")
    return path.read_bytes()


def literal_assignment(raw, name):
    tree = ast.parse(raw.decode("utf-8-sig"))
    definitions = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)]
    if len(definitions) != 1: raise ValueError("Missing or ambiguous adapter declaration: " + name)
    try: return ast.literal_eval(definitions[0].value)
    except (ValueError, TypeError): raise ValueError("Adapter declaration must remain a static literal: " + name) from None


def implementation(root):
    files = {name: read_owned(root, name) for name in TAX_IMPLEMENTATION_FILES}
    task = literal_assignment(files["adapters/toolathlon_native.py"], "TASK_ID")
    methods = literal_assignment(files["integrations/toolathlon_bridge.py"], "ALLOWED")
    valid_name = lambda name: isinstance(name, str) and bool(re.fullmatch("[A-Za-z0-9][A-Za-z0-9_-]{0,127}", name))
    if not valid_name(task) or not isinstance(methods, dict) or not 1 <= len(methods) <= 16:
        raise ValueError("Unsupported task or MCP service declaration")
    for server, names in methods.items():
        if (not valid_name(server) or not isinstance(names, (set, list, tuple)) or not 1 <= len(names) <= 128
            or len(set(names)) != len(names) or any(not valid_name(name) for name in names)):
            raise ValueError("Unsupported MCP method declaration")
    return task, {server: sorted(names) for server, names in sorted(methods.items())}, {
        name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}


def build_registry(root=ROOT):
    task, methods, identities = implementation(root)
    lock = json.loads(read_owned(root, "configs/mainline_sources.lock.json"))
    capabilities = json.loads(read_owned(root, "training/toolathlon_seed_capabilities.json"))
    commit = lock["sources"]["toolathlon_gym"]["commit"]
    row = capabilities.get("tasks", {}).get(task)
    if (not re.fullmatch("[0-9a-f]{40}", commit) or capabilities.get("source") != "toolathlon_gym"
        or capabilities.get("commit") != commit or not row or row.get("configuration_status") != "inspected"):
        raise ValueError("Task profile requires inspected configuration metadata at its pinned source")
    if (not re.fullmatch("[0-9a-f]{40}", row.get("config_git_blob", ""))
        or not re.fullmatch("[0-9a-f]{64}", row.get("config_sha256", ""))
        or not set(methods) <= set(row["needed_mcp_servers"])):
        raise ValueError("Adapter declarations differ from the pinned task configuration")
    profile = {"profile_id": task + "-v1", "source": "toolathlon_gym", "source_commit": commit, "task_id": task,
               "task_config_git_blob": row["config_git_blob"], "task_config_sha256": row["config_sha256"],
               "mcp_methods": methods, "artifact_tools": ["write_report", "write_workbook"],
               "resource_backends": ["shared", "isolated"], "implementation_sha256": identities,
               "declared_services_not_exposed_as_mcp": sorted(set(row["needed_mcp_servers"]) - set(methods)),
               "declared_local_tools_not_exposed": sorted(row["needed_local_tools"]),
               "runtime_activation": "requires_runtime_task_router_validation", "training_admission": False}
    meal = meal_profile(root, commit, capabilities)
    profiles = {profile["profile_id"]: profile, meal["profile_id"]: meal}
    if "wc-order-analysis-gcal" in capabilities["tasks"]:
        order = order_profile(root, commit, capabilities)
        profiles[order["profile_id"]] = order
    if "wc-order-fulfillment-tracker" in capabilities["tasks"]:
        fulfillment = fulfillment_profile(root, commit, capabilities)
        profiles[fulfillment["profile_id"]] = fulfillment
    if "wc-shipping-zone-gsheet" in capabilities["tasks"]:
        shipping = shipping_profile(root, commit, capabilities)
        profiles[shipping["profile_id"]] = shipping
    if "wc-customer-order-gsheet-email" in capabilities["tasks"]:
        vip = vip_profile(root, commit, capabilities)
        profiles[vip["profile_id"]] = vip
    return {"schema_version": "toolathlon-task-adapter-profiles-v1", "profiles": profiles,
            "runtime_scheduler_integrated": True, "automatic_training_admissions": 0}


def meal_profile(root, commit, capabilities):
    files = {name: read_owned(root, name) for name in MEAL_IMPLEMENTATION_FILES}
    declaration = files["integrations/meal_plan_services.py"]
    task, methods = literal_assignment(declaration, "TASK_ID"), literal_assignment(declaration, "METHODS")
    expected = {"howtocook": ("mcp_howtocook_getAllRecipes", "mcp_howtocook_getRecipeById"),
        "google_calendar": ("create_event", "get_event", "update_event", "delete_event", "list_events")}
    if task != "howtocook-meal-plan-gcal" or methods != expected: raise ValueError("Meal task or original method inventory differs")
    row = capabilities.get("tasks", {}).get(task)
    if (not row or row.get("configuration_status") != "inspected"
        or not re.fullmatch("[0-9a-f]{40}", row.get("config_git_blob", ""))
        or not re.fullmatch("[0-9a-f]{64}", row.get("config_sha256", ""))
        or not set(methods) <= set(row["needed_mcp_servers"])):
        raise ValueError("Meal profile requires its pinned inspected configuration")
    return {"profile_id": task + "-v1", "source": "toolathlon_gym", "source_commit": commit, "task_id": task,
        "task_config_git_blob": row["config_git_blob"], "task_config_sha256": row["config_sha256"],
        "mcp_methods": {server: sorted(names) for server, names in sorted(methods.items())},
        "artifact_tools": ["write_workbook", "read_workbook"], "sql_schemas": ["gcal"], "resource_backends": ["isolated"],
        "implementation_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        "declared_services_not_exposed_as_mcp": sorted(set(row["needed_mcp_servers"]) - set(methods)),
        "declared_local_tools_not_exposed": sorted(row["needed_local_tools"]),
        "runtime_activation": "requires_runtime_task_router_validation", "training_admission": False}


def order_profile(root, commit, capabilities):
    files = {name: read_owned(root, name) for name in ORDER_IMPLEMENTATION_FILES}
    declaration = files["integrations/order_analysis_resource.py"]
    methods = literal_assignment(declaration, "METHODS")
    task = "wc-order-analysis-gcal"
    row = capabilities["tasks"][task]
    if (row.get("configuration_status") != "inspected" or not set(methods) <= set(row["needed_mcp_servers"])):
        raise ValueError("Order profile needs inspected pinned configuration")
    return {"profile_id": task + "-v1", "source": "toolathlon_gym", "source_commit": commit, "task_id": task,
        "task_config_git_blob": row["config_git_blob"], "task_config_sha256": row["config_sha256"],
        "mcp_methods": {server: sorted(names) for server, names in sorted(methods.items())},
        "artifact_tools": ["write_workbook", "read_workbook"], "sql_schemas": ["wc", "gcal"], "resource_backends": ["isolated"],
        "implementation_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        "declared_services_not_exposed_as_mcp": sorted(set(row["needed_mcp_servers"]) - set(methods)),
        "declared_local_tools_not_exposed": sorted(row["needed_local_tools"]),
        "runtime_activation": "requires_runtime_task_router_validation", "training_admission": False}


def fulfillment_profile(root, commit, capabilities):
    files = {name: read_owned(root, name) for name in FULFILLMENT_IMPLEMENTATION_FILES}
    task = "wc-order-fulfillment-tracker"
    methods = literal_assignment(files["integrations/fulfillment_resource.py"], "METHODS")
    if methods != {"woocommerce": ("woo_orders_list",), "google_sheet": ("create_spreadsheet", "list_sheets", "rename_sheet", "update_cells", "get_sheet_data")}:
        raise ValueError("Fulfillment original method inventory differs")
    row = capabilities["tasks"][task]
    if row.get("configuration_status") != "inspected" or not set(methods) <= set(row["needed_mcp_servers"]):
        raise ValueError("Fulfillment profile needs inspected pinned configuration")
    return {"profile_id": task + "-v1", "source": "toolathlon_gym", "source_commit": commit, "task_id": task,
        "task_config_git_blob": row["config_git_blob"], "task_config_sha256": row["config_sha256"],
        "mcp_methods": {server: sorted(names) for server, names in sorted(methods.items())},
        "artifact_tools": ["write_workbook", "read_workbook"], "sql_schemas": ["wc", "gsheet"], "resource_backends": ["isolated"],
        "implementation_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        "declared_services_not_exposed_as_mcp": sorted(set(row["needed_mcp_servers"]) - set(methods)),
        "declared_local_tools_not_exposed": sorted(row["needed_local_tools"]),
        "measurement_mode": "project_derived_sql_oracle", "reward_version": "workenv-fulfillment-project-sql-diagnostic-v1",
        "official_benchmark_score_available": False,
        "runtime_activation": "requires_runtime_task_router_validation", "training_admission": False}


def shipping_profile(root, commit, capabilities):
    files = {name: read_owned(root, name) for name in SHIPPING_IMPLEMENTATION_FILES}
    task = "wc-shipping-zone-gsheet"
    methods = literal_assignment(files["integrations/shipping_resource.py"], "METHODS")
    if methods != {"woocommerce": ("woo_orders_list", "woo_shipping_zones_list", "woo_shipping_zone_methods_list", "workenv_shipping_zone_locations_get"), "google_sheet": ("create_spreadsheet", "list_sheets", "rename_sheet", "update_cells", "get_sheet_data")}:
        raise ValueError("Fulfillment original method inventory differs")
    row = capabilities["tasks"][task]
    if row.get("configuration_status") != "inspected" or not set(methods) <= set(row["needed_mcp_servers"]):
        raise ValueError("Fulfillment profile needs inspected pinned configuration")
    return {"profile_id": task + "-v1", "source": "toolathlon_gym", "source_commit": commit, "task_id": task,
        "task_config_git_blob": row["config_git_blob"], "task_config_sha256": row["config_sha256"],
        "mcp_methods": {server: sorted(names) for server, names in sorted(methods.items())},
        "artifact_tools": ["write_workbook", "read_workbook"], "sql_schemas": ["wc", "gsheet"], "resource_backends": ["isolated"],
        "implementation_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        "declared_services_not_exposed_as_mcp": sorted(set(row["needed_mcp_servers"]) - set(methods)),
        "declared_local_tools_not_exposed": sorted(row["needed_local_tools"]),
        "project_read_extensions": ["workenv_shipping_zone_locations_get"], "shipping_locations_enabled": True,
        "measurement_mode": "project_derived_sql_oracle", "reward_version": "workenv-shipping-project-sql-diagnostic-v1",
        "official_benchmark_score_available": False,
        "runtime_activation": "requires_runtime_task_router_validation", "training_admission": False}


def vip_profile(root, commit, capabilities):
    files = {name: read_owned(root,name) for name in VIP_IMPLEMENTATION_FILES}
    task = "wc-customer-order-gsheet-email"
    methods = literal_assignment(files["integrations/vip_email_components.py"], "METHODS")
    expected = {"woocommerce": ("woo_customers_list",), "google_sheet": ("create_spreadsheet", "list_sheets", "rename_sheet", "create_sheet", "update_cells", "get_sheet_data"), "email": ("send_email", "get_emails", "read_email")}
    row = capabilities['tasks'][task]
    if (methods != expected or row.get('configuration_status') != 'inspected'
        or not {'emails' if s=='email' else s for s in methods} <= set(row['needed_mcp_servers'])):raise ValueError('VIP pinned configuration or original method inventory differs')
    return {"profile_id":task+'-v1',"source":"toolathlon_gym","source_commit":commit,"task_id":task,
        "task_config_git_blob":row['config_git_blob'],"task_config_sha256":row['config_sha256'],
        "mcp_methods":{s:sorted(names) for s,names in sorted(methods.items())},"artifact_tools":[],
        "sql_schemas":["wc","gsheet","email"],"resource_backends":["isolated"],
        "implementation_sha256":{n:hashlib.sha256(raw).hexdigest() for n,raw in files.items()},
        "declared_services_not_exposed_as_mcp":sorted(set(row['needed_mcp_servers'])-{'emails' if s=='email' else s for s in methods}),
        "original_service_aliases":{"email":"emails"},
        "declared_local_tools_not_exposed":sorted(row['needed_local_tools']),
        "measurement_mode":"original_database_evaluator_with_project_supplement",
        "reward_version":"workenv-vip-original-db-diagnostic-v1","official_benchmark_score_available":False,
        "runtime_activation":"requires_runtime_task_router_validation","training_admission":False}


def profile_digest(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resolve_profile(registry, *, task_id, source_commit, task_config_sha256, resource_backend, root=ROOT):
    # Rebuild against literal source declarations, never import or execute an adapter.
    if registry != build_registry(root): raise ValueError("Task profile differs from current source/configuration identities")
    matches = [p for p in registry["profiles"].values() if p["task_id"] == task_id]
    if len(matches) != 1: raise ValueError("No unique source-bound adapter profile for the requested task")
    profile = matches[0]
    if (profile["source_commit"] != source_commit or profile["task_config_sha256"] != task_config_sha256
        or resource_backend not in profile["resource_backends"]):
        raise ValueError("Task profile source, configuration or resource backend differs")
    return {"profile": deepcopy(profile), "profile_sha256": profile_digest(profile), "resource_backend": resource_backend,
            "runtime_activation": False, "training_admission": False}


def main():
    registry = build_registry()
    no_links(ROOT / PROFILE_PATH, ROOT)
    save(ROOT / PROFILE_PATH, registry)
    contracts = []
    for profile in registry["profiles"].values():
        for backend in profile["resource_backends"]:
            result = resolve_profile(registry, task_id=profile["task_id"], source_commit=profile["source_commit"],
                                     task_config_sha256=profile["task_config_sha256"], resource_backend=backend)
            contracts.append({"profile_id": profile["profile_id"], "resource_backend": backend,
                              "profile_sha256": result["profile_sha256"], "runtime_activation": False})
    paths = ("integrations/task_profiles.py", PROFILE_PATH, "configs/mainline_sources.lock.json", "training/toolathlon_seed_capabilities.json", *IMPLEMENTATION_FILES)
    receipt = {"schema_version": "task-profile-source-contract-v1", "passed": True, "profiles": len(registry["profiles"]),
               "contracts": contracts, "runtime_scheduler_integrated": True, "training_admissions": 0,
               "real_model_calls": 0, "training_updates": 0,
               "source_sha256": {name: hashlib.sha256(read_owned(ROOT, name)).hexdigest() for name in paths}}
    save(ROOT / "evidence/mainline/task_adapter_profiles.json", receipt)
    print(json.dumps({"profiles": receipt["profiles"], "backend_contracts": len(contracts), "runtime_scheduler_integrated": True,
                      "training_admissions": 0}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
