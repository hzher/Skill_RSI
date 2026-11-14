"""An isolated mutable office service sharing the project task lifecycle."""
import json
import shutil
import uuid

from workenv.core import contained,digest,read_json,save
from workenv.file_adapter import FileTaskAdapter
from workenv.ticket_logic import reference_tickets,verify_tickets
from workenv.ticket_service import SERVICE_SCHEMAS,TicketStore,TicketTools


class TicketTaskAdapter(FileTaskAdapter):
    modes=("known_correct","alternative","old_answer","empty","claim_only","partial","wrong_object","wrong_due","wrong_route",
           "missing_approval","missing_artifact","corrupt_artifact","verifier_error","tool_error","tamper_input","tamper_audit","tamper_closed","file_only","wrong_report")
    extra_tool_schemas=SERVICE_SCHEMAS

    def create(self,mode,run_id):
        run=super().create(mode,run_id)
        metadata=read_json(run/"run.json")
        metadata.update(runtime_adapter="local_tickets",verifier_version="ticket-state-v0.1",resource_artifacts=[])
        save(run/"run.json",metadata)
        return run

    def tools(self,run,trace):return TicketTools(run,trace)

    def inputs(self,run):
        base=run/"private/inputs"
        return {p.relative_to(base).as_posix():p.read_text(encoding="utf-8") for p in base.rglob("*") if p.is_file()}

    def snapshot(self,run):
        state=super().snapshot(run)
        store=TicketStore(run)
        state["service"]=store.snapshot()
        state["resource_files"]={"resources/tickets.sqlite":digest(store.path)}
        return state

    def resource_artifacts(self,run,final):
        return [{"ref":name,"snapshot_key":name} for name in final.get("resource_files",{})]

    def reset(self,run,*,archive=True):
        self.check_private(run)
        resources=contained(run,"resources")
        if resources.exists():
            if not archive:raise ValueError("Fresh initialization cannot reuse a service database")
            destination=contained(run,"resources_before_reset_"+uuid.uuid4().hex[:8])
            # Both absolute paths are checked inside the owned run before moving.
            shutil.move(str(resources),str(destination))
            metadata=read_json(run/"run.json")
            for item in metadata.get("resource_artifacts",[]):
                if item["ref"].startswith("resources/"):item["ref"]=destination.name+"/"+item["ref"].removeprefix("resources/")
            save(run/"run.json",metadata)
        # Base reset dispatches snapshot(), so initialize resource before creating workspace.
        TicketStore(run).initialize(self.inputs(run))
        return super().reset(run,archive=archive)

    async def perform(self,run,mode,trace):
        tools=self.tools(run,trace)
        if mode in ("empty","claim_only"):
            if mode=="claim_only":trace.append({"type":"scripted_claim","text":"Done","is_model_output":False})
            return "scripted_path_finished"
        override={"sla_hours":{"high":4,"normal":24},"approval_categories":["access"]} if mode=="old_answer" else None
        if mode=="file_only":
            # Genuine empty service plus fabricated file delivery: the state verifier must reject it.
            from workenv.ticket_logic import queue_outputs
            inputs=self.inputs(run)
            import csv,io
            policy=json.loads(inputs["policy.json"])
            fabricated=list(csv.DictReader(io.StringIO(inputs["tickets.csv"])))
            for row in fabricated:
                if row["status"]=="open":row["status"]="assigned"
            outputs=queue_outputs(fabricated,policy)
        else:
            outputs=reference_tickets(tools,alternative=mode=="alternative",policy_override=override,
                partial=mode=="partial",wrong_due=mode=="wrong_due",wrong_route=mode=="wrong_route",skip_approval=mode=="missing_approval")
        for name,text in outputs.items():tools.call("write_file",{"path":"output/"+name,"content":text})
        output=run/"workspace/output"
        if mode=="missing_artifact":(output/"queue.csv").unlink()
        elif mode=="corrupt_artifact":(output/"summary.json").write_bytes(b"not JSON")
        elif mode=="wrong_object":(output/"queue.csv").write_text(outputs["queue.csv"].replace("TKT-","OTHER-",1),encoding="utf-8")
        elif mode=="wrong_report":(output/"report.md").write_text("# Done",encoding="utf-8")
        elif mode=="tamper_input":(run/"workspace/inputs/policy.json").write_text("{}",encoding="utf-8")
        elif mode=="tamper_audit":
            with TicketStore(run).connect() as conn:conn.execute("DELETE FROM audit")
        elif mode=="tamper_closed":
            with TicketStore(run).connect() as conn:conn.execute("UPDATE tickets SET title='tampered' WHERE status='closed'")
        elif mode=="tool_error":tools.call("unknown_tool",{})
        return "scripted_path_finished"

    def evaluate(self,run,mode):
        self.check_private(run)
        if mode=="verifier_error":return {"evaluation_status":"error","task_passed":None,"score":None,"reward":None,"error_category":"verifier_infrastructure","check_results":[]}
        final=self.snapshot(run)
        checks=[{"name":"visible_input_unchanged_"+name,"passed":final["files"].get(name)==value} for name,value in self.manifest["asset_sha256"].items()]
        checks+=verify_tickets(self.inputs(run),final["service"],run/"workspace/output")
        return {"evaluation_status":"completed","task_passed":all(c["passed"] for c in checks),"score":None,"reward":None,"error_category":None,"check_results":checks}
