"""Audit latest NeMo journals, source bindings, contract scope and source bundle."""
import hashlib
import argparse
import io
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.mainline_audit import audit_diagnostic, read_json, source_binding, collect_inputs, track_input, track_absence, inventory_snapshot, inputs_current
from integrations.synthetic_audit import audit_synthetic
from integrations.isolation_audit import audit_isolation, audit_episode_recovery
from integrations.rotation_audit import audit_rotation
from integrations.worker_audit import audit_workers
from integrations.isolated_audit import audit_isolated, CHECKS as ISOLATED_CHECKS
from workenv.source_inventory import source_files
from workenv.core import contained
from workenv.handoff import MAX_ARCHIVE_BYTES, MAX_CONTENT_BYTES


def audit_with_inputs(input_state):
    checks, diagnostics = [], []

    def check(name, operation):
        try:
            detail = operation()
            checks.append({"name": name, "passed": True, "detail": detail})
        except Exception as exc:
            checks.append({"name": name, "passed": False, "error_type": type(exc).__name__, "error": str(exc)[:500]})

    def inventory_data_component():
        from integrations.inventory_data_audit import audit
        paths=list((ROOT/'evidence/mainline').glob('inventory-data-*/result.json'))
        if not paths:raise ValueError('Inventory data component evidence missing')
        return audit(ROOT,max(paths,key=lambda p:read_json(p,ROOT).get('at','')))
    check('owned_inventory_product_data_not_task_acceptance',inventory_data_component)
    def inventory_verifier_component():
        from integrations.inventory_evaluation_audit import audit
        paths=list((ROOT/'evidence/mainline').glob('inventory-evaluation-*/result.json'))
        if not paths:raise ValueError('Inventory SQL-fixture verifier evidence missing')
        return audit(ROOT,max(paths,key=lambda p:read_json(p,ROOT).get('at','')))
    check('inventory_original_sql_fixture_verifier_not_tool_task_acceptance',inventory_verifier_component)
    def inventory_original_components():
        from integrations.inventory_components_audit import audit
        paths=list((ROOT/'evidence/mainline').glob('inventory-components-*/result.json'))
        if not paths: raise ValueError('Inventory original three-service evidence missing')
        return audit(ROOT,max(paths,key=lambda p:read_json(p,ROOT).get('at','')))
    check('inventory_original_three_service_components_not_task_acceptance', inventory_original_components)
    def email_identity_contract():
        from integrations.email_task_identity import audit_review, REPORT
        return audit_review(ROOT, ROOT / REPORT)
    check('fixed_email_task_identity_contract_not_tool_integration', email_identity_contract)

    for prefix in ("nemo-diagnostic", "nemo-recovery", "nemo-expiry"):
        def diagnostic(prefix=prefix):
            paths = list((ROOT / "evidence/mainline").glob(prefix + "-*/result.json"))
            if not paths: raise ValueError("Missing diagnostic")
            path = max(paths, key=lambda p: read_json(p, ROOT).get("at", ""))
            return audit_diagnostic(ROOT, path)
        check(prefix, diagnostic)

    def synthetic():
        paths = list((ROOT / "evidence/mainline").glob("nemo-synthetic-*/result.json"))
        if not paths: raise ValueError("Missing synthetic NeMo diagnostic")
        return audit_synthetic(ROOT, max(paths, key=lambda p: read_json(p, ROOT).get("at", "")))
    check("nemo_synthetic_diagnostics", synthetic)
    def isolation():
        paths = list((ROOT / "evidence/mainline").glob("episode-cluster-probe-*/result.json"))
        if not paths: raise ValueError("Missing isolated original-task resource probe")
        return audit_isolation(ROOT, max(paths, key=lambda p: read_json(p, ROOT).get("at", "")))
    check("original_task_resource_isolation", isolation)
    def episode_recovery():
        paths = list((ROOT / "evidence/mainline").glob("episode-recovery-*/result.json"))
        if not paths: raise ValueError("Missing owned worker fault probe")
        return audit_episode_recovery(ROOT, max(paths, key=lambda p: read_json(p, ROOT).get("at", "")))
    check("owned_episode_worker_recovery", episode_recovery)
    def ledger_rotation(backend):
        paths = list((ROOT / "evidence/mainline").glob("ledger-rotation-*/result.json"))
        paths = [p for p in paths if read_json(p, ROOT).get("ledger", "synthetic") == backend]
        if not paths: raise ValueError("Missing actual NeMo ledger rotation diagnostic")
        return audit_rotation(ROOT, max(paths, key=lambda p: read_json(p, ROOT).get("at", "")), backend=backend)
    check("retired_identity_preserving_rotation", lambda: ledger_rotation("synthetic"))
    check("isolated_retired_identity_preserving_rotation", lambda: ledger_rotation("isolated"))
    def private_workers():
        paths = list((ROOT / "evidence/mainline").glob("episode-workers-*/result.json"))
        if not paths: raise ValueError("Missing private resource worker diagnostic")
        return audit_workers(ROOT, max(paths, key=lambda p: read_json(p, ROOT).get("at", "")))
    check("private_resource_workers", private_workers)
    for schema in ISOLATED_CHECKS:
        def isolated(schema=schema):
            paths = [p for p in (ROOT / "evidence/mainline").glob("nemo-isolated-*/result.json") if read_json(p, ROOT).get("schema_version") == schema]
            if not paths: raise ValueError("Missing isolated NeMo diagnostic: " + schema)
            return audit_isolated(ROOT, max(paths, key=lambda p: read_json(p, ROOT).get("at", "")))
        check(schema, isolated)

    def contracts():
        receipt = read_json(ROOT / "evidence/mainline/contracts.json", ROOT)
        if not receipt.get("passed") or receipt["failures"] or receipt["errors"]: raise ValueError("Contracts did not pass")
        source_binding(ROOT, receipt["source_sha256"])
        expected = {p.relative_to(ROOT).as_posix() for folder in ("integrations", "training", "evaluation") for p in (ROOT / folder).rglob("*.py")}
        if not expected <= set(receipt["source_sha256"]): raise ValueError("New mainline modules were added after contract collection")
        return {"tests_run": receipt["tests_run"], "skipped": receipt["skipped"]}
    check("contract_scope_and_bytes", contracts)

    def prompt_metadata():
        from training.prompt_blob_overlap import compare_prompts
        from evaluation.firewall import digest
        receipt = read_json(ROOT / "evidence/mainline/prompt_blob_overlap.json", ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        manifest = read_json(ROOT / "evaluation/toolathlon_verified.manifest.json", ROOT)
        catalog = read_json(ROOT / "training/toolathlon_seed_catalog.json", ROOT)
        gym, verified = receipt["gym_prompt_objects"], receipt["verified_prompt_objects"]
        if (receipt["evaluation_manifest_sha256"] != digest(manifest) or receipt["gym_commit"] != catalog["commit"]
            or receipt["verified_commit"] != manifest["commit"] or set(gym) != {row["task_id"] for row in catalog["directories"]}
            or set(verified) & set(receipt["verified_non_task_metadata_objects"])
            or set(verified) | set(receipt["verified_non_task_metadata_objects"]) != set(manifest["task_ids"])):
            raise ValueError("Prompt metadata does not cover its pinned manifests")
        for key, value in compare_prompts(gym, verified).items():
            if receipt.get(key) != value: raise ValueError("Prompt-object comparison disagrees with its metadata")
        if receipt["gym_prompt_count"] != len(gym) or receipt["verified_prompt_count"] != len(verified):
            raise ValueError("Prompt-object counts disagree with their inventories")
        return {"gym_tasks": len(gym), "verified_tasks_with_prompts": len(verified),
                "overlapping_gym_tasks": receipt["overlapping_gym_tasks"], "automatic_admissions": 0}
    check("prompt_object_metadata_and_source_pins", prompt_metadata)

    def training_source_api():
        from training.source_api import inspect_sources, report_current
        report = read_json(ROOT / "evidence/mainline/training_source_api.json", ROOT)
        source_binding(ROOT, report["source_sha256"])
        if not report_current(ROOT, report) or report != inspect_sources(ROOT):
            raise ValueError("Source API report differs from the pinned Git objects or current adapter source")
        return {"static_checks": len(report["checks"]), "frameworks_imported": False,
                "runtime_compatibility_verified": False, "ready_for_training": False}
    check("pinned_training_source_api", training_source_api)

    def office_sources():
        from training.office_task_review import inspect_all
        receipt = read_json(ROOT / "evidence/mainline/office_task_reviews.json", ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        if receipt != inspect_all(ROOT): raise ValueError("Office public source review changed")
        return {"tasks_inspected": len(receipt["tasks"]), "new_runtime_profiles": 0, "reference_workbooks_read": False}
    check("office_candidate_public_source_inspection", office_sources)

    def customer_components():
        from integrations.customer_report_audit import audit_components
        paths = list((ROOT / "evidence/mainline").glob("customer-components-*/result.json"))
        if not paths: raise ValueError("Missing customer component diagnostic")
        return audit_components(ROOT, max(paths, key=lambda p: read_json(p, ROOT)["at"]))
    check("original_customer_report_components_not_task_acceptance", customer_components)

    def fulfillment_components():
        from integrations.fulfillment_audit import audit_components
        paths = list((ROOT / "evidence/mainline").glob("fulfillment-components-*/result.json"))
        if not paths: raise ValueError("Missing fulfillment component diagnostic")
        return audit_components(ROOT, max(paths, key=lambda p: read_json(p, ROOT)["at"]))
    check("original_fulfillment_components_not_task_acceptance", fulfillment_components)

    def fulfillment_nemo():
        from integrations.fulfillment_runtime_audit import audit_fulfillment
        paths = list((ROOT / "evidence/mainline").glob("nemo-fulfillment-*/result.json"))
        paths = [p for p in paths if read_json(p,ROOT).get("schema_version") == "nemo-fulfillment-project-diagnostic-v1"]
        if not paths: raise ValueError("Missing actual fulfillment NeMo diagnostic")
        return audit_fulfillment(ROOT,max(paths,key=lambda p:read_json(p,ROOT)["at"]))
    check("actual_nemo_fulfillment_project_diagnostic", fulfillment_nemo)

    def fulfillment_lifecycle():
        from integrations.fulfillment_runtime_audit import audit_lifecycle
        results = {}
        for kind in ("recovery", "expiry"):
            paths = list((ROOT / "evidence/mainline").glob("nemo-fulfillment-"+kind+"-*/result.json"))
            if not paths: raise ValueError("Missing actual fulfillment " + kind)
            results[kind] = audit_lifecycle(ROOT,max(paths,key=lambda p:read_json(p,ROOT)["at"]))
        return results
    check("actual_nemo_fulfillment_lifecycle", fulfillment_lifecycle)

    def shipping_sources():
        from training.shipping_task_review import inspect
        receipt = read_json(ROOT / "evidence/mainline/shipping_task_review.json", ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        if receipt != inspect(ROOT): raise ValueError("Shipping public service review differs")
        return {"public_sources": len(receipt["public_sources"]), "original_location_tool_missing": True, "reference_answers_read": False}
    check("shipping_public_service_and_read_extension_review", shipping_sources)

    def shipping_components():
        from integrations.shipping_audit import audit_components
        paths = list((ROOT / "evidence/mainline").glob("shipping-components-*/result.json"))
        if not paths: raise ValueError("Missing shipping component diagnostic")
        return audit_components(ROOT, max(paths, key=lambda p: read_json(p, ROOT)["at"]))
    check("original_shipping_components_not_task_acceptance", shipping_components)

    def email_public_review():
        from training.email_task_review import audit_review
        return audit_review(ROOT, ROOT / "evidence/mainline/email_task_review.json")
    check("email_next_task_public_source_review_only", email_public_review)

    def email_build():
        from integrations.email_audit import audit_build
        return audit_build(ROOT, ROOT / "evidence/mainline/email_build.json")
    check("original_email_separate_locked_pg_build", email_build)

    def email_components():
        from integrations.email_audit import audit_components
        paths = list((ROOT / "evidence/mainline").glob("email-components-*/result.json"))
        if not paths: raise ValueError("Missing original PG email component diagnostic")
        return audit_components(ROOT, max(paths, key=lambda p: read_json(p, ROOT)["at"]))
    check("original_email_pg_components_not_task_acceptance", email_components)

    def vip_components():
        from integrations.vip_email_audit import audit_components
        paths = list((ROOT / "evidence/mainline").glob("vip-components-*/result.json"))
        if not paths: raise ValueError("Missing complete original VIP business component diagnostic")
        return audit_components(ROOT, max(paths, key=lambda p: read_json(p, ROOT)["at"]))
    check("original_vip_business_components_not_task_acceptance", vip_components)

    def vip_resource_preparation():
        from integrations.vip_resource_audit import audit_preparation
        paths = list((ROOT / "evidence/mainline").glob("vip-resource-adapter-*/result.json"))
        if not paths: raise ValueError("Missing direct VIP resource adapter preparation diagnostic")
        return audit_preparation(ROOT,max(paths,key=lambda p:read_json(p,ROOT)["at"]))
    check("vip_resource_adapter_preparation_not_nemo_acceptance", vip_resource_preparation)

    def vip_nemo():
        from integrations.vip_runtime_audit import audit_vip
        paths=list((ROOT/'evidence/mainline').glob('nemo-vip-*/result.json'))
        paths=[p for p in paths if read_json(p,ROOT).get('schema_version')=='nemo-vip-project-diagnostic-v1']
        if not paths:raise ValueError('Missing actual NeMo VIP cloud/mail diagnostic')
        return audit_vip(ROOT,max(paths,key=lambda p:read_json(p,ROOT)['at']))
    check('actual_nemo_vip_cloud_mail_diagnostic',vip_nemo)

    def vip_basic_lifecycle():
        from integrations.vip_runtime_audit import audit_lifecycle
        result={}
        for kind in ('recovery','expiry'):
            paths=list((ROOT/'evidence/mainline').glob('nemo-vip-'+kind+'-*/result.json'))
            if not paths:raise ValueError('Missing VIP basic '+kind)
            result[kind]=audit_lifecycle(ROOT,max(paths,key=lambda p:read_json(p,ROOT)['at']))
        return {**result,'verification_scope':'bridge_loss_and_lease_expiry_only'}
    check('actual_nemo_vip_basic_lifecycle',vip_basic_lifecycle)

    def vip_worker_fault():
        from integrations.vip_fault_audit import audit_fault
        paths=list((ROOT/'evidence/mainline').glob('nemo-vip-worker-fault-*/result.json'))
        if not paths:raise ValueError('Missing VIP inflight private-worker fault diagnostic')
        return audit_fault(ROOT,max(paths,key=lambda p:read_json(p,ROOT)['at']))
    check('actual_nemo_vip_worker_fault',vip_worker_fault)

    def inventory_public_review():
        from training.inventory_task_review import audit_review,REPORT
        return audit_review(ROOT,ROOT/REPORT)
    check('inventory_next_task_public_source_review_only',inventory_public_review)

    def shipping_nemo():
        from integrations.shipping_runtime_audit import audit_shipping
        paths = list((ROOT / "evidence/mainline").glob("nemo-shipping-*/result.json"))
        paths = [p for p in paths if read_json(p,ROOT).get("schema_version") == "nemo-shipping-project-diagnostic-v1"]
        if not paths: raise ValueError("Missing actual shipping NeMo diagnostic")
        return audit_shipping(ROOT,max(paths,key=lambda p:read_json(p,ROOT)["at"]))
    check("actual_nemo_shipping_project_diagnostic", shipping_nemo)

    def shipping_lifecycle():
        from integrations.shipping_runtime_audit import audit_lifecycle
        results = {}
        for kind in ("recovery", "expiry"):
            paths = list((ROOT / "evidence/mainline").glob("nemo-shipping-"+kind+"-*/result.json"))
            if not paths: raise ValueError("Missing actual shipping " + kind)
            results[kind] = audit_lifecycle(ROOT,max(paths,key=lambda p:read_json(p,ROOT)["at"]))
        return results
    check("actual_nemo_shipping_lifecycle", shipping_lifecycle)

    def typed_verified_tasks():
        from evaluation.task_inventory import build_index, load_index, INDEX_PATH
        receipt = read_json(ROOT / "evidence/mainline/verified_task_inventory.json", ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        index = read_json(ROOT / INDEX_PATH, ROOT)
        loaded, checksum = load_index(ROOT)
        if (not receipt.get("passed") or index != loaded or index != build_index(ROOT)
            or receipt["task_index_sha256"] != checksum or receipt["task_directories"] != len(index["task_ids"])
            or receipt["non_task_entries"] != len(index["non_task_metadata_objects"])):
            raise ValueError("Typed Verified inventory differs from pinned Git tree metadata")
        return {"task_directories": receipt["task_directories"], "metadata_files": receipt["non_task_entries"],
                "solutions_loaded": False, "evaluation_executed": False}
    check("typed_verified_task_inventory", typed_verified_tasks)

    def seed_capability_metadata():
        from training.seed_capabilities import inspect_capabilities, CONFIG_PATH
        receipt = read_json(ROOT / "evidence/mainline/seed_capabilities.json", ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        report = read_json(ROOT / CONFIG_PATH, ROOT)
        if not receipt.get("passed") or report != inspect_capabilities(ROOT):
            raise ValueError("Seed capability metadata differs from the pinned locally available config objects")
        if any(receipt.get(name) != value for name, value in report["summary"].items()):
            raise ValueError("Seed capability summary differs from its inventory")
        return {"task_directories": receipt["task_directories"], "configurations_inspected": receipt["configurations_inspected"],
                "configuration_unknown": receipt["configuration_unknown"], "admitted_training_seeds": 0}
    check("seed_service_capability_metadata", seed_capability_metadata)

    def task_profile_contracts():
        from integrations.task_profiles import build_registry, profile_digest, PROFILE_PATH
        receipt = read_json(ROOT / "evidence/mainline/task_adapter_profiles.json", ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        registry = read_json(ROOT / PROFILE_PATH, ROOT)
        expected = [{"profile_id": p["profile_id"], "resource_backend": backend, "profile_sha256": profile_digest(p),
                     "runtime_activation": False} for p in registry["profiles"].values() for backend in p["resource_backends"]]
        if (not receipt.get("passed") or registry != build_registry(ROOT) or receipt.get("contracts") != expected
            or receipt.get("profiles") != len(registry["profiles"]) or receipt.get("runtime_scheduler_integrated") is not True):
            raise ValueError("Task profile differs from current pinned configuration and literal adapter declarations")
        return {"profiles": receipt["profiles"], "backend_contracts": len(expected), "runtime_scheduler_integrated": True,
                "training_admissions": 0}
    check("source_bound_task_profile_contracts", task_profile_contracts)

    def actual_task_routing():
        from integrations.task_routing import TaskRouter
        paths = list((ROOT / "evidence/mainline").glob("task-routing-probe-*/result.json"))
        if not paths: raise ValueError("Missing actual NeMo task-routing probe")
        path = max(paths, key=lambda p: read_json(p, ROOT).get("at", ""))
        receipt = read_json(path, ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        required = {backend + ":" + name for backend in ("shared", "isolated") for name in
                    ("unknown_task", "foreign_source", "payload_rejected", "rejected_before_allocation", "source_bound_tools", "artifact_write", "persisted_profile", "closed")}
        if (receipt.get("schema_version") != "nemo-task-routing-diagnostic-v1" or receipt.get("passed") is not True
            or len(receipt["checks"]) != 16 or {r["name"] for r in receipt["checks"]} != required
            or receipt.get("execution_kind") != "scripted_resource_diagnostic"
            or any(r["passed"] is not True for r in receipt["checks"])
            or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates", "training_admissions"))):
            raise ValueError("Incomplete task-routing diagnostic")
        journal_path = path.parent / "http_journal.json"
        journal = read_json(journal_path, ROOT)
        if hashlib.sha256(journal_path.read_bytes()).hexdigest() != receipt["journal_sha256"]:
            raise ValueError("Task-routing journal identity changed")
        for backend, prefix in (("shared", ""), ("isolated", "/isolated")):
            events = journal[backend]
            if (len(events) != 7 or [row["status"] for row in events] != [400, 400, 400, 200, 200, 200, 200]
                or [row["route"] for row in events] != [prefix + "/seed_session"] * 4 + [prefix + "/tools", prefix + "/write_report", prefix + "/close_session"]):
                raise ValueError("Task-routing calls differ from the actual acceptance sequence")
            seed = events[3]["request"]
            if events[4]["response"]["task_profile"] != TaskRouter(ROOT, backend).authorize(seed):
                raise ValueError("NeMo exposed a different task profile")
            observation = receipt["allocation_observations"][backend]
            if type(observation["before"]) is not int or observation["before"] < 0 or observation["before"] != observation["after"]:
                raise ValueError("Rejected seeds allocated a resource")
            import base64
            report = base64.b64decode(events[5]["request"]["base64"], validate=True)
            if events[5]["response"]["result"]["sha256"] != hashlib.sha256(report).hexdigest():
                raise ValueError("Artifact bytes differ from the requested report")
            reference = receipt["profile_files"][backend]
            if not reference["ref"].startswith("runs/") or not reference["ref"].endswith("/task_profile.json"):
                raise ValueError("Unexpected retained profile reference")
            profile_path = contained(ROOT, reference["ref"])
            retained = read_json(profile_path, ROOT)
            if hashlib.sha256(profile_path.read_bytes()).hexdigest() != reference["sha256"] or retained != TaskRouter(ROOT, backend).authorize(seed):
                raise ValueError("Persisted profile identity differs from the actual resource")
            if any(row["request"]["resources_session_id"] != seed["resources_session_id"] for row in events):
                raise ValueError("Task-routing episode identity changed")
        return {"actual_backends": ["shared", "isolated"], "checks": 16, "training_admissions": 0}
    check("actual_source_bound_task_routing", actual_task_routing)

    def next_adapter_design():
        from training.adapter_design import design_adapter
        receipt = read_json(ROOT / "evidence/mainline/next_seed_adapter_design.json", ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        expected = design_adapter(ROOT, receipt["task_id"])
        if any(receipt.get(k) != value for k, value in expected.items()):
            raise ValueError("Next seed design differs from current pinned public configuration")
        return {"task_id": expected["task_id"], "runtime_activation": False, "training_admission": False,
                "missing_declared_services": expected["declared_services_without_existing_mcp_profile"]}
    check("next_original_task_adapter_design", next_adapter_design)

    def second_task_lifecycle():
        from training.task_lifecycle_design import inspect_lifecycle
        receipt = read_json(ROOT / "evidence/mainline/next_seed_lifecycle.json", ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        expected = inspect_lifecycle(ROOT)
        if any(receipt.get(k) != value for k, value in expected.items()):
            raise ValueError("Second-task lifecycle design differs from pinned public source")
        return {"task_id": expected["task_id"], "source_blobs": len(expected["source_objects"]), "runtime_activation": False}
    check("second_original_task_lifecycle_design", second_task_lifecycle)

    def public_recipe_snapshot():
        from training.recipe_snapshot import verify_snapshot
        from training.task_lifecycle_design import PUBLIC_RECIPE_MEMBER, pinned_sources
        receipt = read_json(ROOT / "evidence/mainline/howtocook_recipe_snapshot.json", ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        if (receipt.get("passed") is not True or receipt.get("runtime_activation") is not False
            or receipt.get("training_admission") is not False or receipt.get("mcp_service_started") is not False):
            raise ValueError("Public recipe snapshot promoted to runtime or training")
        summary = verify_snapshot(ROOT, receipt)
        track_input(contained(ROOT, receipt["asset_ref"]), ROOT, summary["sha256"])
        source = read_json(ROOT / "configs/mainline_sources.lock.json", ROOT)["sources"]["toolathlon_gym"]
        _, identities = pinned_sources(ROOT, source, members={"recipe_snapshot": PUBLIC_RECIPE_MEMBER}, max_bytes=2 * 1024 * 1024)
        if identities["recipe_snapshot"] != receipt["source_object"]:
            raise ValueError("Public snapshot no longer matches its pinned original Git path")
        return {"recipe_count": summary["recipe_count"], "bytes": summary["bytes"], "runtime_activation": False}
    check("immutable_public_recipe_snapshot", public_recipe_snapshot)

    def original_recipe_mcp():
        from integrations.howtocook_component import verify_component, NETWORK_GUARD
        build = read_json(ROOT / "evidence/mainline/howtocook_build.json", ROOT)
        source_binding(ROOT, build["source_sha256"])
        base = verify_component(ROOT, build)
        from scripts.build_howtocook_offline import validate_dependency_lock
        dependency_identity = validate_dependency_lock(read_json(ROOT / "configs/howtocook.package.json", ROOT),
            read_json(ROOT / "configs/howtocook.package-lock.json", ROOT))
        if build.get("dependency_identity") != dependency_identity: raise ValueError("Original recipe dependency identity differs")
        for name, checksum in build["compiled_sha256"].items(): track_input(contained(base, name), ROOT, checksum)
        paths = list((ROOT / "evidence/mainline").glob("howtocook-probe-*/result.json"))
        if not paths: raise ValueError("Missing original offline recipe MCP diagnostic")
        path = max(paths, key=lambda p: read_json(p, ROOT).get("at", ""))
        receipt = read_json(path, ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        required = {"owned_original_node_process", "original_tool_schemas", "all_pinned_recipes_returned", "original_recipe_lookup",
                    "unknown_recipe_is_explicit", "invalid_argument_rejected", "owned_process_closed", "no_instrumented_network_requests"}
        if (receipt.get("passed") is not True or receipt.get("schema_version") != "original-howtocook-offline-mcp-diagnostic-v1"
            or receipt.get("execution_kind") != "original_mcp_component_diagnostic"
            or len(receipt["checks"]) != 8 or {r["name"] for r in receipt["checks"]} != required
            or any(r["passed"] is not True for r in receipt["checks"])
            or receipt.get("nemo_second_task_enabled") is not False or receipt.get("os_network_sandbox") is not False
            or receipt.get("runtime_task_activated") is not False or receipt["build_fingerprint"] != build["build_fingerprint"]
            or receipt["compiled_sha256"] != build["compiled_sha256"]
            or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates"))):
            raise ValueError("Incomplete or promoted original recipe MCP diagnostic")
        journal_path = path.parent / "tool_journal.json"
        events = read_json(journal_path, ROOT)
        if hashlib.sha256(journal_path.read_bytes()).hexdigest() != receipt["journal_sha256"] or len(events) != 4:
            raise ValueError("Original recipe MCP journal identity changed")
        if [e["name"] for e in events] != ["mcp_howtocook_getAllRecipes"] + ["mcp_howtocook_getRecipeById"] * 3:
            raise ValueError("Original recipe MCP calls changed")
        listing = json.loads(events[0]["response"]["content"][0]["text"])
        snapshot = read_json(ROOT / "evidence/mainline/howtocook_recipe_snapshot.json", ROOT)
        if len(listing) != snapshot["recipe_count"] or events[3]["response"].get("protocol_error", {}).get("code") != -32602:
            raise ValueError("Original recipe MCP response coverage or invalid-argument behavior changed")
        guard = path.parent / "network_guard.mjs"
        if guard.read_text(encoding="utf-8") != NETWORK_GUARD: raise ValueError("Offline diagnostic network guard changed")
        track_input(guard, ROOT)
        observation = read_json(path.parent / "network_observation.json", ROOT)
        if observation.get("instrumented_network_calls") != 0 or observation["pid"] != receipt["node_process_identity"]["pid"]:
            raise ValueError("Original recipe MCP attempted an instrumented network request")
        return {"recipe_count": len(listing), "checks": 8, "typecheck_completed": build["typecheck_completed"],
                "dependency_identity": dependency_identity,
                "compilation_mode": build["compilation_mode"], "nemo_second_task_enabled": False}
    check("original_offline_recipe_mcp_component", original_recipe_mcp)

    def original_calendar_mcp():
        from integrations.calendar_audit import audit_calendar
        paths = list((ROOT / "evidence/mainline").glob("calendar-probe-*/result.json"))
        if not paths: raise ValueError("Missing original calendar MCP diagnostic")
        path = max(paths, key=lambda p: read_json(p, ROOT).get("at", ""))
        return audit_calendar(ROOT, path)
    check("original_calendar_episode_component", original_calendar_mcp)

    def original_meal_plan_local_evaluation():
        from copy import deepcopy
        import re
        from verify_meal_plan_evaluation import CHECKS
        from integrations.episode_cluster import EpisodeProfile
        from integrations.mainline_audit import track_absence
        from adapters.meal_plan_evaluation import classify_original
        from adapters.meal_plan_validation import measure_workbook
        paths = list((ROOT / "evidence/mainline").glob("meal-evaluation-probe-*/result.json"))
        if not paths: raise ValueError("Missing local meal-plan evaluator diagnostic")
        path = max(paths, key=lambda p: read_json(p, ROOT).get("at", ""))
        receipt = read_json(path, ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        if (receipt.get("passed") is not True or receipt.get("schema_version") != "original-meal-plan-local-evaluation-probe-v1"
            or receipt.get("execution_kind") != "scripted_local_component_diagnostic"
            or [c["name"] for c in receipt["checks"]] != list(CHECKS) or any(c["passed"] is not True for c in receipt["checks"])
            or any(receipt.get(k) is not False for k in ("reference_solutions_read", "verified_task_bodies_read", "runtime_task_activated", "nemo_second_task_enabled", "training_eligible"))
            or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("real_model_calls", "training_updates"))):
            raise ValueError("Incomplete or promoted local meal-plan diagnostic")
        profile = EpisodeProfile(ROOT, receipt["profile"]["resource_id"], 0)
        profile.validate_record(receipt["profile"])
        if read_json(profile.base / "cluster_owner.json", ROOT) != profile.record(): raise ValueError("Meal-plan episode owner changed")
        track_absence(profile.data / "postmaster.pid", ROOT)
        if receipt["shared_before"] != receipt["shared_after"]: raise ValueError("Meal-plan probe changed shared resources")
        if not 19 <= len(receipt["retained_sha256"]) <= 32: raise ValueError("Unexpected retained meal-plan inventory")
        for name, checksum in receipt["retained_sha256"].items():
            member = contained(ROOT, name)
            if not name.startswith(".runtime/native-episodes/") or member.stat().st_size > 256 * 1024 or hashlib.sha256(member.read_bytes()).hexdigest() != checksum:
                raise ValueError("Retained meal-plan evidence changed")
            track_input(member, ROOT, checksum)
        stages = {"empty": (False, False, False), "positive": (True, True, True), "wrong_recipe": (False, True, False),
                  "restored": (True, True, True), "database_stopped": (None, None, None)}
        if set(receipt["evaluations"]) != set(stages): raise ValueError("Meal-plan evaluation coverage differs")
        source = read_json(ROOT / "configs/mainline_sources.lock.json", ROOT)["sources"]["toolathlon_gym"]
        from training.task_lifecycle_design import pinned_sources, MEMBERS
        original, identities = pinned_sources(ROOT, source, members={"evaluation": MEMBERS["evaluation"]})
        for stage, expected in stages.items():
            evaluation_path = contained(ROOT, receipt["evaluations"][stage]["ref"])
            measured = read_json(evaluation_path, ROOT)
            source_binding(ROOT, measured["source_sha256"])
            if (tuple(measured[k] for k in ("task_passed", "upstream_passed", "supplemental_passed")) != expected
                or measured["evaluation_status"] != ("error" if stage == "database_stopped" else "completed")
                or measured["profile"] != profile.record() or measured["reward"] is not None or measured["training_eligible"] is not False
                or measured["nemo_task_executed"] is not False): raise ValueError("Meal-plan result classification differs")
            if stage != "database_stopped":
                raw = evaluation_path.parent / "original_evaluation.py"
                stdout = evaluation_path.parent / "original.stdout.txt"
                stderr = evaluation_path.parent / "original.stderr.txt"
                if (raw.read_bytes() != original["evaluation"] or measured["original_evaluator_source"] != identities["evaluation"]
                    or measured["original_evaluator_executed"] is not True
                    or classify_original(measured["upstream_exit_code"], stdout.read_text(encoding="utf-8"), stderr.read_text(encoding="utf-8")) != expected[1]):
                    raise ValueError("Original meal-plan evaluator source or output differs")
        journal_path = path.parent / "tool_journal.json"
        journal = read_json(journal_path, ROOT)
        if (hashlib.sha256(journal_path.read_bytes()).hexdigest() != receipt["journal_sha256"] or len(journal) != 23
            or [e["name"] for e in journal] != ["create_event"] * 21 + ["update_event"] * 2
            or any(e["response"].get("isError") for e in journal)): raise ValueError("Original meal-plan calendar calls differ")
        events = []
        for call in journal[:21]:
            match = re.match(r"Event created with ID: ([0-9a-f-]+)\n", call["response"]["content"][0]["text"])
            if not match: raise ValueError("Invalid original event creation response")
            events.append({"id": match[1], **call["arguments"]})
        snapshot = read_json(ROOT / "evidence/mainline/howtocook_recipe_snapshot.json", ROOT)
        recipes = read_json(contained(ROOT, snapshot["asset_ref"]), ROOT)
        if receipt["recipe_sha256"] != snapshot["sha256"]: raise ValueError("Meal-plan public recipe binding differs")
        workspace = contained(ROOT, receipt["workbook_ref"]).parent
        artifact = receipt["artifact_write"]
        raw = (workspace / "Weekly_Meal_Plan.xlsx").read_bytes()
        if (artifact["schema_version"] != "meal-plan-artifact-write-v1" or artifact["profile"] != profile.record()
            or artifact["artifact_ref"] != receipt["workbook_ref"] or artifact["bytes"] != len(raw)
            or artifact["sha256"] != hashlib.sha256(raw).hexdigest() or artifact["task_content_evaluated"] is not False):
            raise ValueError("Bounded workbook transport receipt differs from retained artifact")
        intents = [name for name in receipt["retained_sha256"] if "/meal-artifact-write-" in name]
        if len(intents) != 1 or read_json(contained(ROOT, intents[0]), ROOT) != {**artifact, "state": "completed"}:
            raise ValueError("Workbook write intent did not complete consistently")
        if measure_workbook(events, workspace, recipes)["task_passed"] is not True: raise ValueError("Retained meal-plan artifacts no longer pass supplement")
        corrupted = deepcopy(events)
        if any(call["arguments"]["eventId"] != events[0]["id"] for call in journal[21:]): raise ValueError("Wrong-recipe probe changed another event")
        corrupted[0]["summary"] = journal[21]["arguments"]["summary"]
        if measure_workbook(corrupted, workspace, recipes)["task_passed"] is not False or journal[22]["arguments"]["summary"] != events[0]["summary"]:
            raise ValueError("Wrong-recipe rejection/restoration differs")
        session = receipt["session"]
        before, after = session["before"], session["after"]
        closed = after["status"] in ("not_found", "exited") or bool(after.get("creation_id") and after["creation_id"] != before["creation_id"])
        if session.get("closed") is not True or session["profile"] != profile.record() or before["status"] != "alive" or not before.get("creation_id") or not closed:
            raise ValueError("Meal-plan calendar child closure differs")
        return {"checks": len(CHECKS), "original_evaluations": 4, "calendar_calls": 23, "nemo_second_task_enabled": False}
    check("original_meal_plan_local_evaluators", original_meal_plan_local_evaluation)

    def original_meal_plan_services():
        from integrations.meal_services_audit import audit_services
        paths = list((ROOT / "evidence/mainline").glob("meal-services-probe-*/result.json"))
        if not paths: raise ValueError("Missing original meal-plan business service diagnostic")
        path = max(paths, key=lambda p: read_json(p, ROOT).get("at", ""))
        return audit_services(ROOT, path)
    check("original_meal_plan_business_services", original_meal_plan_services)

    def original_meal_plan_dependent_recovery():
        from integrations.meal_recovery_audit import audit_recovery
        paths = list((ROOT / "evidence/mainline").glob("meal-recovery-probe-*/result.json"))
        if not paths: raise ValueError("Missing original meal-plan dependent recovery diagnostic")
        path = max(paths, key=lambda p: read_json(p, ROOT).get("at", ""))
        return audit_recovery(ROOT, path)
    check("original_meal_plan_dependent_recovery", original_meal_plan_dependent_recovery)

    def original_meal_plan_private_workers():
        from integrations.meal_worker_audit import audit_workers
        paths = list((ROOT / "evidence/mainline").glob("meal-workers-probe-*/result.json"))
        if not paths: raise ValueError("Missing original meal-plan private worker diagnostic")
        path = max(paths, key=lambda p: read_json(p, ROOT).get("at", ""))
        return audit_workers(ROOT, path)
    check("original_meal_plan_private_workers", original_meal_plan_private_workers)

    def actual_nemo_meal_plan():
        from integrations.nemo_meal_audit import audit_nemo_meal
        paths = list((ROOT / "evidence/mainline").glob("nemo-meal-plan-*/result.json"))
        if not paths: raise ValueError("Missing actual NeMo meal-plan task diagnostic")
        path = max(paths, key=lambda p: read_json(p, ROOT).get("at", ""))
        return audit_nemo_meal(ROOT, path)
    check("actual_nemo_original_meal_plan", actual_nemo_meal_plan)

    def actual_nemo_meal_lifecycle():
        from integrations.nemo_meal_audit import audit_meal_lifecycle
        details = []
        for prefix in ("nemo-meal-recovery-*", "nemo-meal-expiry-*"):
            paths = list((ROOT / "evidence/mainline").glob(prefix + "/result.json"))
            if not paths: raise ValueError("Missing NeMo meal lifecycle diagnostic")
            path = max(paths, key=lambda p: read_json(p, ROOT).get("at", ""))
            details.append(audit_meal_lifecycle(ROOT, path))
        return {"diagnostics": details, "nemo_recovery_and_expiry_tested": True}
    check("actual_nemo_meal_lifecycle", actual_nemo_meal_lifecycle)

    def actual_order():
        from integrations.order_analysis_audit import audit_order
        paths=list((ROOT / "evidence/mainline").glob("nemo-order-analysis-*/result.json"))
        if not paths: raise ValueError("Missing real NeMo order-analysis diagnostic")
        return audit_order(ROOT,max(paths,key=lambda p:read_json(p,ROOT).get("at","")))
    check("actual_nemo_original_order_analysis",actual_order)

    def order_lifecycle():
        from integrations.order_analysis_audit import audit_lifecycle
        details=[]
        for prefix in ("nemo-order-recovery-*","nemo-order-expiry-*"):
            paths=list((ROOT / "evidence/mainline").glob(prefix+"/result.json"))
            if not paths: raise ValueError("Missing real NeMo order-analysis lifecycle diagnostic")
            details.append(audit_lifecycle(ROOT,max(paths,key=lambda p:read_json(p,ROOT).get("at",""))))
        return {"diagnostics":details,"nemo_recovery_and_expiry_tested":True}
    check("actual_nemo_order_analysis_lifecycle",order_lifecycle)

    def actual_nemo_mixed_tasks():
        from integrations.nemo_mixed_audit import audit_mixed
        paths = list((ROOT / "evidence/mainline").glob("nemo-mixed-tasks-*/result.json"))
        if not paths: raise ValueError("Missing actual NeMo mixed original task diagnostic")
        path = max(paths, key=lambda p: read_json(p, ROOT).get("at", ""))
        return audit_mixed(ROOT, path)
    check("actual_nemo_mixed_original_tasks", actual_nemo_mixed_tasks)

    def actual_nemo_meal_worker_fault():
        from integrations.meal_worker_recovery_audit import audit_worker_fault
        paths = list((ROOT / "evidence/mainline").glob("nemo-meal-worker-fault-*/result.json"))
        if not paths: raise ValueError("Missing actual NeMo meal hard-worker fault diagnostic")
        path = max(paths, key=lambda p: read_json(p, ROOT).get("at", ""))
        return audit_worker_fault(ROOT, path)
    check("actual_nemo_meal_hard_worker_fault", actual_nemo_meal_worker_fault)

    def actual_nemo_response_budget():
        from integrations.response_budget_audit import audit_response_budget
        paths = list((ROOT / "evidence/mainline").glob("nemo-response-budget-*/result.json"))
        if not paths: raise ValueError("Missing actual NeMo response budget diagnostic")
        return audit_response_budget(ROOT, max(paths, key=lambda p: read_json(p, ROOT).get("at", "")))
    check("actual_nemo_response_budget", actual_nemo_response_budget)

    def actual_cold_startup():
        from integrations.startup_audit import audit_cold_startup
        return audit_cold_startup(ROOT, ROOT / "evidence/mainline/cold_startup.json")
    check("actual_idle_cold_startup_and_original_tasks", actual_cold_startup)

    def canonical_candidate():
        from training.candidate_seeds import build_candidate, TASK
        from training.source_api import pinned_blob
        receipt = read_json(ROOT / "evidence/mainline/seed_candidate.json", ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        candidate_path = contained(ROOT, receipt["candidate_ref"])
        candidate = read_json(candidate_path, ROOT)
        if hashlib.sha256(candidate_path.read_bytes()).hexdigest() != receipt["candidate_sha256"]:
            raise ValueError("Canonical candidate bytes changed")
        lock = read_json(ROOT / "configs/mainline_sources.lock.json", ROOT)
        source = lock["sources"]["toolathlon_gym"]
        prefix = "tasks/finalpool/" + TASK + "/"
        instruction, instruction_meta = pinned_blob(ROOT, source, prefix + "docs/task.md")
        config, config_meta = pinned_blob(ROOT, source, prefix + "task_config.json")
        expected = build_candidate(instruction.decode("utf-8"), source["commit"], hashlib.sha256(instruction).hexdigest(), hashlib.sha256(config).hexdigest())
        expected.update(source_read_mode="pinned_git_blob_bytes", git_source_files={"docs/task.md": instruction_meta, "task_config.json": config_meta})
        if (candidate != expected or receipt.get("source_read_mode") != expected["source_read_mode"]
            or receipt.get("git_source_files") != expected["git_source_files"]):
            raise ValueError("Candidate differs from the pinned original Git bytes")
        profiles = read_json(ROOT / "configs/task_adapter_profiles.json", ROOT)["profiles"]
        matches = [p for p in profiles.values() if p["task_id"] == TASK]
        if len(matches) != 1 or matches[0]["task_config_sha256"] != expected["source_identity"]["task_config_sha256"]:
            raise ValueError("Candidate and adapter profile configuration bytes disagree")
        archive_ref = receipt.get("previous_candidate_archive")
        if archive_ref:
            import re
            if not re.fullmatch(r"training/candidate_history/[0-9a-f]{64}\.json", archive_ref):
                raise ValueError("Unsupported previous candidate archive reference")
            path = contained(ROOT, archive_ref)
            previous = read_json(path, ROOT)
            if (hashlib.sha256(path.read_bytes()).hexdigest() != path.stem
                or previous.get("candidate_status") != "quarantined_not_admitted"):
                raise ValueError("Previous candidate history identity or admission boundary changed")
        return {"source_read_mode": expected["source_read_mode"], "profile_configuration_matches": True,
                "previous_candidate_retained": bool(archive_ref), "training_admissions": 0}
    check("canonical_candidate_git_bytes_and_history", canonical_candidate)

    def generator_profile_admission():
        from training.profile_admission import inspect_admission
        receipt = read_json(ROOT / "evidence/mainline/task_profile_admission.json", ROOT)
        source_binding(ROOT, receipt["source_sha256"])
        observed = inspect_admission(ROOT, receipt["resource_backend"])
        if not receipt.get("passed") or any(receipt.get(k) != value for k, value in observed.items()):
            raise ValueError("Generator profile admission report differs from current source and reviewed provenance")
        return {"checked_admitted_seeds": observed["checked_admitted_seeds"], "new_training_admissions": 0,
                "source_profile_catalog_current": observed["source_profile_catalog_current"], "ready_for_training": False}
    check("generator_dataset_profile_admission", generator_profile_admission)

    def metrics():
        from report_source_metrics import collect_metrics
        receipt = read_json(ROOT / "evidence/mainline/source_metrics.json", ROOT)
        observed = collect_metrics()
        for name, value in observed["files"].items(): track_input(contained(ROOT, name), ROOT, value["sha256"])
        for key in ("code_totals", "categories", "files", "manifest_sha256"):
            if receipt.get(key) != observed[key]: raise ValueError("Source metrics no longer describe the current bytes")
        return receipt["code_totals"]
    check("source_metrics_current_bytes", metrics)

    def source_bundle():
        receipt = read_json(ROOT / "evidence/mainline/source_bundle.json", ROOT)
        verified = read_json(ROOT / "evidence/mainline/source_bundle_verification.json", ROOT)
        bundle = contained(ROOT, receipt["bundle"])
        if not receipt["bundle"].startswith("evidence/handoff/") or bundle.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Unsupported source bundle path/size")
        raw = bundle.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        track_input(bundle, ROOT, actual)
        if actual != receipt["sha256"] or actual != verified["bundle_sha256"] or not verified["passed"]:
            raise ValueError("Bundle/verification SHA disagreement")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(entry.file_size for entry in entries) > MAX_CONTENT_BYTES:
                raise ValueError("Source archive content budget exceeded")
            manifest = json.loads(archive.read("BUNDLE_MANIFEST.json"))
            for path in source_files(ROOT, include_mainline=True):
                name = path.relative_to(ROOT).as_posix()
                # Documentation/status and summaries can legitimately be updated after a snapshot.
                if name.startswith(("docs/", "reports/")) or name in ("README.md", ".gitignore"): continue
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                track_input(path, ROOT, checksum)
                if manifest["files"].get(name) != checksum:
                    raise ValueError("Current program/data source differs from bundle: " + name)
        return {"sha256": actual, "fresh_stdlib_venv": verified["fresh_stdlib_venv"], "nemo_reinstalled": False}
    check("bundle_matches_current_program", source_bundle)
    def stable_inventory():
        if inventory_snapshot(ROOT) != input_state["inventory"]:
            raise ValueError("Program or receipt inventory changed during the audit")
        if input_state.get("selection_draft"):
            from integrations.archive_inputs import draft_inputs_current
            return draft_inputs_current(ROOT, input_fields(input_state))
        if not inputs_current(ROOT, input_fields(input_state)):
            raise ValueError("Audit inputs changed or exceeded their accounting budget")
        return True
    if "archive" in input_state:
        check("archive_authority_originals_and_logical_budget", lambda: input_fields(input_state)["input_budget"])
    elif input_state.get("selection_draft"):
        check("archive_authority_originals_and_logical_budget", lambda: input_fields(input_state)["selection_draft"])
    check("input_inventory_stable", stable_inventory)
    result = {"schema_version": "mainline-consistency-audit-v1", "passed": all(row["passed"] for row in checks),
              "checks": checks, "real_model_calls": 0, "training_updates": 0, "independent_evaluation_scores": None,
              "contracts_sha256": hashlib.sha256((ROOT / "evidence/mainline/contracts.json").read_bytes()).hexdigest(),
              **input_fields(input_state),
              "input_inventory": input_state["inventory"],
              "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
                  "scripts/audit_mainline.py", "integrations/mainline_audit.py", "integrations/synthetic_audit.py", "integrations/isolation_audit.py", "integrations/rotation_audit.py", "integrations/worker_audit.py", "integrations/isolated_audit.py")}}
    if input_state.get("selection_draft"):
        from integrations.archive_inputs import DRAFT_SCHEMA, DRAFT, capture
        from workenv.core import save
        result.update(schema_version=DRAFT_SCHEMA, checks_passed=result["passed"], passed=False)
        capture(ROOT,result,"selection-drafts")
        save(ROOT/DRAFT,result)
        print(json.dumps({"selection_draft":DRAFT,"checks_passed":result["checks_passed"],"passed":False}))
        return 0 if result["checks_passed"] else 1
    (ROOT / "evidence/mainline/consistency_audit.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


def input_fields(state):
    fields = {"input_sha256": dict(state["files"]), "expected_absent": sorted(state["missing"])}
    if "archive" in state:
        from integrations.archive_inputs import fields as archived_fields
        fields.update(archived_fields(ROOT, state["files"], state["archive"], state["archived"]))
    elif state.get("selection_draft"):
        from integrations.archive_inputs import draft_fields
        fields.update(draft_fields(ROOT,state["files"]),input_inventory=state["inventory"])
    return fields


def run_audit(archive_ref=None, *, selection_draft=False):
    from integrations.archive_inputs import POLICY, read_policy
    policy = read_policy(ROOT)
    with collect_inputs(ROOT, archive_ref=archive_ref, selection_draft=selection_draft) as input_state:
        if read_policy(ROOT) != policy: raise ValueError("Archive policy changed while starting the audit")
        if policy is None: track_absence(contained(ROOT, POLICY), ROOT)
        else:
            if read_json(contained(ROOT, POLICY), ROOT) != policy: raise ValueError("Archive policy changed during collection")
            read_json(contained(ROOT, policy["comparison_ref"]), ROOT)
        input_state["inventory"] = inventory_snapshot(ROOT)
        try:
            return audit_with_inputs(input_state)
        except (OSError,ValueError,KeyError,TypeError) as error:
            if not selection_draft: raise
            from integrations.archive_inputs import DRAFT,capture
            from workenv.core import save
            failure={"schema_version":"evidence-selection-draft-failure-v1","passed":False,"checks_passed":False,
                "selection_draft":True,"error_type":type(error).__name__,"error":str(error)[:500],
                "observed_input_sha256":dict(input_state["seen"])}
            capture(ROOT,failure,"selection-failures")
            save(ROOT/DRAFT,failure)
            print(json.dumps({"selection_draft":DRAFT,"passed":False,"error":failure["error"]}))
            return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--raw-inputs", action="store_true")
    mode.add_argument("--evidence-archive", metavar="PRIVATE_REF")
    mode.add_argument("--compare-evidence-archive", metavar="PRIVATE_REF")
    mode.add_argument("--compare-current-archive", metavar="PRIVATE_REF", help="Compare selected grouped authority with a rebase candidate")
    mode.add_argument("--selection-draft", action="store_true", help="Run all checks on bounded actual inputs; never write a passing audit")
    mode.add_argument("--compare-selection-draft", metavar="PRIVATE_REF", help="Compare fixed fresh draft to a candidate ordinary audit")
    parser.add_argument("--activate", action="store_true", help="Activate only after a successful same-source comparison")
    parser.add_argument("--expected-policy-sha256", help="Required compare-and-swap digest for rebase activation")
    args = parser.parse_args()
    if args.activate and not (args.compare_evidence_archive or args.compare_current_archive or args.compare_selection_draft):
        parser.error("--activate requires an explicit archive comparison")
    if (args.compare_current_archive or args.compare_selection_draft) and args.activate and not args.expected_policy_sha256:
        parser.error("Rebase --activate requires --expected-policy-sha256 from the reviewed policy")
    if args.expected_policy_sha256 and not ((args.compare_current_archive or args.compare_selection_draft) and args.activate):
        parser.error("--expected-policy-sha256 requires rebase comparison and activation")
    from integrations.archive_inputs import read_policy, compare, compare_archives, activate, capture
    policy = read_policy(ROOT)
    if args.selection_draft: return run_audit(selection_draft=True)
    if args.compare_selection_draft:
        from integrations.archive_inputs import DRAFT,draft_current,compare_draft
        from training.runtime_capabilities import project_capabilities
        from workenv.core import save
        from integrations.evidence_archive import _bytes,_json,MAX_MANIFEST
        if policy is None: parser.error("Draft candidate activation requires an existing owned policy")
        draft=read_json(ROOT/DRAFT,ROOT)
        draft_current(ROOT,draft)
        target=ROOT/"evidence/mainline/consistency_audit.json"
        if not target.is_file():
            parser.error("Draft comparison requires a retained ordinary audit to restore on failure")
        previous=_json(_bytes(ROOT,target.relative_to(ROOT).as_posix(),MAX_MANIFEST)) if target.exists() else None
        if previous is not None: capture(ROOT,previous)
        activated=False
        try:
            if run_audit(args.compare_selection_draft): raise ValueError("Draft candidate ordinary audit failed")
            after=read_json(target,ROOT)
            summary=project_capabilities(ROOT)["summary"]
            comparison=compare_draft(ROOT,draft,after,summary)
            capture(ROOT,comparison,"comparisons")
            if args.activate:
                activate(ROOT,after,comparison,expected_policy_sha256=args.expected_policy_sha256)
                activated=True
            save(ROOT/"evidence/mainline/archive_draft_comparison.json",comparison)
            print(json.dumps({"draft_comparison":comparison,"activated":activated}))
        finally:
            if activated:
                selected=read_policy(ROOT)
                if selected is None or run_audit(selected["archive_ref"]):
                    raise ValueError("Selected ordinary audit failed after draft activation")
            elif previous is not None: save(target,previous)
        return 0
    if args.compare_current_archive:
        if policy is None: parser.error("Grouped rebase comparison requires an existing selected policy")
        from training.runtime_capabilities import project_capabilities
        target = ROOT / "evidence/mainline/consistency_audit.json"
        if target.is_file(): capture(ROOT, json.loads(target.read_text(encoding="utf-8")))
        if run_audit(policy["archive_ref"]): return 1
        before = json.loads(target.read_text(encoding="utf-8"))
        before_summary = project_capabilities(ROOT)["summary"]
        try:
            if run_audit(args.compare_current_archive): raise ValueError("Candidate archive audit did not pass")
            after = json.loads(target.read_text(encoding="utf-8"))
            after_summary = project_capabilities(ROOT)["summary"]
            comparison = compare_archives(ROOT, before, after, before_summary, after_summary)
            capture(ROOT, comparison, "comparisons")
            if args.activate:
                activate(ROOT, after, comparison, expected_policy_sha256=args.expected_policy_sha256)
            (ROOT / "evidence/mainline/archive_rebase_comparison.json").write_text(json.dumps(comparison, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"archive_rebase_comparison": comparison, "activated": args.activate}, indent=2))
        finally:
            # Keep the public current audit on the selected authority after a
            # comparison, including a failed one. Corrupt policy never falls back.
            selected = read_policy(ROOT)
            if selected is None or run_audit(selected["archive_ref"]):
                raise ValueError("Selected archive audit failed after rebase comparison")
        return 0
    if not args.compare_evidence_archive:
        ref = args.evidence_archive or (None if args.raw_inputs or policy is None else policy["archive_ref"])
        return run_audit(ref)
    from training.runtime_capabilities import project_capabilities
    target = ROOT / "evidence/mainline/consistency_audit.json"
    if target.is_file(): capture(ROOT, json.loads(target.read_text(encoding="utf-8")))
    if run_audit(): return 1
    raw = json.loads(target.read_text(encoding="utf-8"))
    raw_summary = project_capabilities(ROOT)["summary"]
    if run_audit(args.compare_evidence_archive): return 1
    grouped = json.loads(target.read_text(encoding="utf-8"))
    grouped_summary = project_capabilities(ROOT)["summary"]
    comparison = compare(ROOT, raw, grouped, raw_summary, grouped_summary)
    capture(ROOT, comparison, "comparisons")
    if args.activate: activate(ROOT, grouped, comparison)
    (ROOT / "evidence/mainline/archive_input_comparison.json").write_text(json.dumps(comparison, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"archive_input_comparison": comparison, "activated": args.activate}, indent=2))
    return run_audit(args.compare_evidence_archive) if args.activate else 0


if __name__ == "__main__": raise SystemExit(main())
