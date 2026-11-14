"""Fulfillment task declarations over the shared owned office resource lifecycle."""
from adapters.fulfillment_validation import TASK_ID, HEADERS
from adapters.fulfillment_evaluation import evaluate_episode
from integrations.fulfillment_components import FulfillmentComponents, initialize, snapshot
from integrations.office_report_resource import OfficeReportResourceAdapter

METHODS = {"woocommerce": ("woo_orders_list",),
           "google_sheet": ("create_spreadsheet", "list_sheets", "rename_sheet", "update_cells", "get_sheet_data")}


class FulfillmentResourceAdapter(OfficeReportResourceAdapter):
    task_id, artifact_task, run_name = TASK_ID, "fulfillment", "fulfillment-task-session"
    component_type, methods, headers = FulfillmentComponents, METHODS, HEADERS
    row_limits, text_columns = {"Order Status": 256, "Fulfillment Summary": 16}, ("Status", "Metric")
    workbook_ref = "fulfillment-workspace/Fulfillment_Report.xlsx"

    def source_snapshot(self):
        return snapshot(self.cluster)

    def evaluate_episode(self):
        return evaluate_episode(self.root, self.cluster)

    def initialize(self):
        return initialize(self.root, self.cluster)
