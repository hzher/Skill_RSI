"""Shipping task declarations over the shared owned office resource lifecycle."""
from workenv.core import ResourceError, read_json
from adapters.shipping_validation import TASK_ID, HEADERS
from adapters.shipping_evaluation import evaluate_episode
from integrations.shipping_components import ShippingComponents, initialize, snapshot, configuration_digest
from integrations.office_report_resource import OfficeReportResourceAdapter

METHODS = {"woocommerce": ("woo_orders_list", "woo_shipping_zones_list", "woo_shipping_zone_methods_list", "workenv_shipping_zone_locations_get"),
           "google_sheet": ("create_spreadsheet", "list_sheets", "rename_sheet", "update_cells", "get_sheet_data")}


class ShippingResourceAdapter(OfficeReportResourceAdapter):
    task_id, artifact_task, run_name = TASK_ID, "shipping", "shipping-task-session"
    component_type, methods, headers = ShippingComponents, METHODS, HEADERS
    row_limits, text_columns = {"Zone Summary": 256, "Methods": 4096}, ("Zone_Name", "Zone", "Method", "Details")
    workbook_ref = "shipping-workspace/Shipping_Analysis.xlsx"

    def source_snapshot(self):
        data = snapshot(self.cluster)
        record = read_json(self.root / ".runtime/nemo-vm/shipping-seed.json")
        if configuration_digest(data) != record.get("shipping_configuration_sha256"):
            raise ResourceError("Shipping restored configuration differs from seed binding")
        return data

    def evaluate_episode(self):
        return evaluate_episode(self.root, self.cluster)

    def initialize(self):
        return initialize(self.root, self.cluster)
