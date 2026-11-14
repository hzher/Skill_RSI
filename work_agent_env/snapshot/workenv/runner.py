"""One lifecycle for registered native and file task adapters."""
from __future__ import annotations

import asyncio
from pathlib import Path
import time
import traceback

from workenv.core import ROOT, load_task, owned_run, read_json, run_directory, run_lock, save, now,partial_snapshot

DEFAULT_TASK = "wc-sales-tax-summary"


def adapter_for(spec: dict):
    if spec["runtime_adapter"] == "local_tickets":
        from workenv.ticket_adapter import TicketTaskAdapter
        return TicketTaskAdapter(spec)
    if spec["runtime_adapter"] == "native_business_check":
        from workenv.native_adapter import NativeTaxAdapter
        return NativeTaxAdapter(spec)
    if spec["runtime_adapter"] == "local_files":
        from workenv.file_adapter import FileTaskAdapter
        return FileTaskAdapter(spec)
    raise ValueError("Unsupported runtime adapter")


async def execute(mode="known_correct", run_id=None, *, task_id=DEFAULT_TASK, timeout=150):
    if timeout <= 0:
        raise ValueError("Timeout must be positive")
    adapter = adapter_for(load_task(task_id))
    if mode not in adapter.modes:
        raise ValueError("Mode not supported by task: " + mode)
    started = time.monotonic()
    with adapter.guard():
        run = adapter.create(mode, run_id)
        with run_lock(run):
            metadata, trace = read_json(run / "run.json"), []
            try:
                initial = adapter.reset(run, archive=False)
                metadata = read_json(run / "run.json")
                save(run / "initial_state.json", initial)
                termination = await asyncio.wait_for(adapter.perform(run, mode, trace), timeout=timeout)
                save(run / "trace.json", trace)
                final = adapter.snapshot(run)
                save(run / "final_state.json", final)
                result = adapter.evaluate(run, mode)
                save(run / "evaluation.json", result)
                metadata.update(result)
                metadata.update(execution_status="completed", termination_reason=termination,
                                artifact_refs=["workspace/"+name for name in final["files"] if not name.startswith("inputs/") and name != "task.md"],
                                check_results="evaluation.json")
                if hasattr(adapter,"resource_artifacts"):metadata["resource_artifacts"]=adapter.resource_artifacts(run,final)
            except Exception as exc:
                save(run / "trace.json", trace)
                (run / "diagnostic_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
                metadata.update(execution_status="error", evaluation_status="not_completed", task_passed=None,
                                score=None, reward=None, termination_reason="execution_timeout" if isinstance(exc,TimeoutError) else "diagnostic_error",
                                error_category="infrastructure_or_adapter", error=type(exc).__name__+": "+str(exc))
                try:
                    final = adapter.snapshot(run)
                    save(run / "final_state.json", final)
                    metadata["artifact_refs"] = ["workspace/"+name for name in final["files"] if not name.startswith("inputs/") and name != "task.md"]
                    if hasattr(adapter,"resource_artifacts"):metadata["resource_artifacts"]=adapter.resource_artifacts(run,final)
                except Exception as capture_error:
                    metadata["final_state_capture_error"] = str(capture_error)
                    try:
                        final=partial_snapshot(run,capture_error)
                        save(run/"final_state.json",final)
                        metadata["artifact_refs"]=["workspace/"+name for name in final["files"] if not name.startswith("inputs/") and name!="task.md"]
                    except Exception as fallback_error:metadata["partial_capture_error"]=str(fallback_error)
            finally:
                metadata.update(end_time=now(), latency=time.monotonic()-started)
                save(run / "run.json", metadata)
            return metadata


def inspect_adapter(run_id):
    run = run_directory(run_id)
    metadata = owned_run(run)
    return run, adapter_for(load_task(metadata["task_id"]))


def reset_run(run_id):
    run, adapter = inspect_adapter(run_id)
    with adapter.guard(), run_lock(run):
        before = adapter.snapshot(run)
        after = adapter.reset(run)
        metadata = read_json(run / "run.json")
        resources_preserved=all((run/item["ref"]).is_file() for item in metadata.get("resource_artifacts",[]))
        preserved = all((run / ref).is_file() for ref in metadata["artifact_refs"]) and resources_preserved
        result = {"run_id":run_id, "before":before, "after":after,
                  "matches_initial":after == read_json(run / "initial_state.json"),
                  "archived_artifacts_preserved":preserved,"archived_resources_preserved":resources_preserved,
                  "resource_artifacts_count":len(metadata.get("resource_artifacts",[])), "agent_context_reset":"N/A: deterministic diagnostics", "memory_reset":"disabled"}
        save(run / "reset_verification.json", result)
        return result


def evaluate_run(run_id):
    run, adapter = inspect_adapter(run_id)
    with adapter.guard(), run_lock(run):
        if adapter.snapshot(run) != read_json(run / "final_state.json"):
            raise RuntimeError("Current resources do not match saved final state")
        result = adapter.evaluate(run, "known_correct")
        save(run / ("evaluation_recheck_" + __import__("uuid").uuid4().hex[:8] + ".json"), result)
        return result
