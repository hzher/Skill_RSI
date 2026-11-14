"""Adapt the validated tax task to the common local execution contract."""
from __future__ import annotations

from adapters import toolathlon_native as native
from workenv.native_operations import MODES, mutate


class NativeTaxAdapter:
    modes = MODES

    def __init__(self, spec: dict):
        if spec["task_id"] != native.TASK_ID:
            raise ValueError("Native tax adapter supports only its pinned task")
        self.spec = spec

    def guard(self):
        return native.task_lock()

    def create(self, mode, run_id):
        run = native.new_run(mode, run_id)
        metadata = __import__("json").loads((run / "run.json").read_text(encoding="utf-8"))
        metadata["runtime_adapter"] = "native_business_check"
        metadata["code_file_sha256"] = __import__("workenv.core", fromlist=["source_hashes"]).source_hashes()
        native.save(run / "run.json", metadata)
        return run

    def reset(self, run, *, archive=True):
        return native.reset(run, archive=archive)

    def snapshot(self, run):
        return native.snapshot(run / "workspace")

    async def perform(self, run, mode, trace):
        if mode in ("empty", "claim_only"):
            if mode == "claim_only":
                trace.append({"type":"scripted_claim", "text":"Task completed", "is_model_output":False})
            return "scripted_path_finished"
        business = await native.correct_business_path(run, trace)
        native.save(run / "business_path.json", business)
        if mode == "tool_error":
            async with native.mcp_client("woocommerce", run) as client:
                await native.call(client, "diagnostic_nonexistent_tool", {}, trace)
        elif mode == "extra_shared_tab":
            async with native.mcp_client("google_sheet", run) as client:
                await native.call(client, "create_sheet", {"spreadsheet_id":business["spreadsheet_id"],"title":"Extra"}, trace)
        else:
            mutate(run, mode)
        return "scripted_path_finished"

    def evaluate(self, run, mode):
        return native.evaluate(run, inject_infrastructure_error=mode == "verifier_error")
