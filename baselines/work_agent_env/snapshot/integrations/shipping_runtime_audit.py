"""Shipping task-specific checks over shared office HTTP and lifecycle auditing."""
import json
from adapters.shipping_evaluation import episode_result, REFERENCE_MODE, REWARD_VERSION
from integrations.shipping_audit import audit_measurement
from integrations.shipping_components import check_original_sources, configuration_digest, METHODS
from integrations.mainline_audit import read_json

CHECKS = ('shared_backend_refused_before_allocation', 'two_owned_nemo_resources', 'original_methods_and_explicit_diagnostic_profile', 'cookie_binding_and_mutation_allowlist', 'missing_artifacts_are_known_failures', 'complete_orders_zones_locations_and_methods', 'two_project_derived_measurements_pass', 'workbook_roundtrip', 'workbook_cent_error_rejected_and_isolated', 'cloud_cent_error_rejected_and_isolated', 'zero_shipping_cost_omission_rejected', 'deduplicated_write_restores_success', 'foreign_ready_marker_is_masked_unknown', 'pending_write_is_masked_unknown', 'closing_one_preserves_other', 'meal_task_reuses_released_slot', 'closed_identity_cannot_reseed', 'owned_children_and_postgres_closed', 'shared_database_and_other_ledgers_unchanged')
EXTRA_SOURCES = ('scripts/verify_nemo_shipping.py', 'integrations/shipping_runtime_audit.py', 'integrations/shipping_audit.py', 'scripts/verify_calendar_isolation.py', 'integrations/office_runtime_audit.py')


def audit_shipping(root, path):
    from integrations.office_runtime_audit import audit_report
    return audit_report(root, path, family="shipping", task_id="wc-shipping-zone-gsheet", checks=CHECKS,
        extra_sources=EXTRA_SOURCES, episode_result=episode_result, audit_measurement=audit_measurement,
        check_inventory=check_inventory, reward_version=REWARD_VERSION, reference_mode=REFERENCE_MODE, min_blindspots=3)


def audit_lifecycle(root, path):
    from integrations.office_runtime_audit import audit_lifecycle as common_lifecycle
    return common_lifecycle(root, path, family="shipping", task_id="wc-shipping-zone-gsheet")


def check_inventory(root, profile, data):
    run = profile.base / "shipping-components-run"
    calls = read_json(run / "tool_journal.json", root)
    schemas = read_json(run / "tool_schemas.json", root)
    if {(s["server"],s["name"]) for s in schemas} != {(s,n) for s,names in METHODS.items() for n in names}:
        raise ValueError("Shipping original/project method inventory differs")
    def events(name): return [e for e in calls if e["server"] == "woocommerce" and e["name"] == name]
    def decode(event): return json.loads("".join(b["text"] for b in event["response"]["content"] if b["type"] == "text"))
    pages = events("woo_orders_list")
    if not pages or [e["arguments"]["page"] for e in pages] != list(range(1,len(pages)+1)) or len(decode(pages[-1])) >= 10:
        raise ValueError("Shipping original order pagination incomplete")
    zones = events("woo_shipping_zones_list")
    if len(zones) != 1: raise ValueError("Shipping zone inventory differs")
    actual = {"orders": [r for e in pages for r in decode(e)], "shipping_zones": decode(zones[0])}
    for name,table in (("woo_shipping_zone_methods_list","shipping_zone_methods"),("workenv_shipping_zone_locations_get","shipping_zone_locations")):
        rows = events(name)
        if sorted(e["arguments"]["zoneId"] for e in rows) != sorted(z["id"] for z in actual["shipping_zones"]):
            raise ValueError("Shipping per-zone calls differ")
        actual[table] = [r for e in rows for r in decode(e)]
    check_original_sources(data, actual)
    seed = read_json(root / ".runtime/nemo-vm/shipping-seed.json", root)
    if configuration_digest(data) != seed["shipping_configuration_sha256"]:
        raise ValueError("Shipping runtime configuration differs from immutable seed binding")
