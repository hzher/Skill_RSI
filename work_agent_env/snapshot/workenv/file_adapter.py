"""Independent file workspaces with frozen verifier inputs and recoverable artifacts."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
import csv
import io
import json
from pathlib import Path
import shutil
import uuid

from workenv.core import ROOT, TZ, contained, digest, file_snapshot, now, owned_run, read_json, run_directory, save, source_hashes
from workenv.file_tools import FileTools
from workenv.office_logic import reference, verify
from workenv.taskpacks import csv_text


class FileTaskAdapter:
    modes=("known_correct","empty","claim_only","wrong_object","partial","wrong_amount","corrupt_artifact",
           "missing_artifact","allowed_format","old_answer","tamper_input","verifier_error","tool_error",
           "alternative","replay_success","replay_no_write","replay_bad_path","replay_budget","replay_timeout","replay_recover",
           "wrong_citation","fabricated_quote","wrong_report")

    def __init__(self,spec:dict):
        from workenv.catalog import validate_pack
        validate_pack(spec)
        self.spec=spec
        self.pack=contained(ROOT,spec["source_path"])
        self.manifest=read_json(self.pack/"task.json")
        if self.manifest["task_id"]!=spec["task_id"] or any(self.manifest[k]!=spec[k] for k in self.manifest):
            raise ValueError("Registry/manifest mismatch")
        for name,value in self.manifest["asset_sha256"].items():
            if digest(contained(self.pack,name))!=value:
                raise ValueError("Immutable task input differs from manifest")

    def guard(self):
        return nullcontext()

    def tools(self,run,trace):
        return FileTools(run/"workspace",trace)

    def create(self,mode,run_id):
        if mode=="old_answer" and self.spec["variant"]=="base":
            raise ValueError("Old-answer check requires a changed-constraint variant")
        run_id=run_id or (datetime.now(TZ).strftime("%Y%m%dT%H%M%S")+"_file_"+uuid.uuid4().hex[:10])
        run=run_directory(run_id)
        run.mkdir(parents=True,exist_ok=False)
        metadata={"run_id":run_id,"workspace_owner":str(ROOT),"task_id":self.spec["task_id"],"task_version":self.spec["task_version"],
                  "task_family":self.spec["family"],"world_id":self.spec["world_id"],"split":self.spec["split"],
                  "leakage_group_id":self.spec["leakage_group_id"],"runtime_adapter":"local_files","execution_mode":mode,
                  "execution_kind":"replay_test_double" if mode.startswith("replay_") else "scripted_business_check",
                  "model_id":None,"policy_version":None,"code_commit":None,"harness_version":"local-contract-v0.2",
                  "verifier_version":"office-constraints-v0.1","config_ref":"tasks/registry.json",
                  "memory_mode":"disabled","skill_version":None,"sequence_id":None,"sequence_index":None,
                  "memory_snapshot_ref":None,"training_capture_ref":None,"reward":None,"reward_version":None,"usage":None,
                  "score":None,"task_passed":None,"execution_status":"in_progress","evaluation_status":"not_run",
                  "termination_reason":None,"error_category":None,"start_time":now(),"end_time":None,
                  "initial_state_ref":"initial_state.json","trajectory_ref":"trace.json","artifact_refs":[],"code_file_sha256":source_hashes()}
        save(run/"run.json",metadata)
        private=run/"private"
        private.mkdir()
        shutil.copytree(self.pack/"inputs",private/"inputs")
        shutil.copyfile(self.pack/"task.md",private/"task.md")
        save(private/"manifest.json",self.manifest)
        return run

    def check_private(self,run):
        metadata=owned_run(run)
        if metadata["task_id"]!=self.spec["task_id"] or read_json(run/"private/manifest.json")!=self.manifest:
            raise ValueError("Private verifier identity changed")
        for name,value in self.manifest["asset_sha256"].items():
            if digest(contained(run/"private",name))!=value:
                raise ValueError("Private verifier resources were modified")

    def snapshot(self,run):
        owned_run(run)
        return {"files":file_snapshot(run/"workspace")}

    def reset(self,run,*,archive=True):
        self.check_private(run)
        metadata=owned_run(run)
        workspace=contained(run,"workspace")
        archived=None
        if workspace.exists():
            if not archive:
                raise ValueError("Fresh initialization cannot reuse an existing workspace")
            archived=contained(run,"workspace_before_reset_"+uuid.uuid4().hex[:8])
            # Both absolute paths verified inside the owned run before the move.
            shutil.move(str(workspace),str(archived))
        workspace.mkdir()
        shutil.copytree(run/"private/inputs",workspace/"inputs")
        shutil.copyfile(run/"private/task.md",workspace/"task.md")
        (workspace/"output").mkdir()
        metadata.setdefault("reset_events",[]).append({"at":now(),"archive_ref":archived.name if archived else None})
        if archived:
            metadata["artifact_refs"]=[archived.name+"/"+r.removeprefix("workspace/") if r.startswith("workspace/") else r for r in metadata["artifact_refs"]]
        save(run/"run.json",metadata)
        return self.snapshot(run)

    async def perform(self,run,mode,trace):
        tools=FileTools(run/"workspace",trace)
        if mode in ("empty","claim_only"):
            if mode=="claim_only":
                trace.append({"type":"scripted_claim","text":"Done","is_model_output":False})
            return "scripted_path_finished"
        input_root=run/"workspace/inputs"
        inputs={p.relative_to(input_root).as_posix():tools.call("read_file",{"path":"inputs/"+p.relative_to(input_root).as_posix()})
                for p in sorted(input_root.rglob("*")) if p.is_file()}
        if mode=="old_answer":
            policy=json.loads(inputs["policy.json"])
            if self.spec["family"]=="receivables": policy["payment_cutoff"]="2026-10-05"
            elif self.spec["family"]=="meeting": policy["not_before_hour"]=9
            else: policy["as_of"]="2026-10-15"
            inputs["policy.json"]=json.dumps(policy)
        outputs=reference(self.spec["family"],inputs,alternative=mode=="alternative")
        if mode.startswith("replay_"):
            from workenv.policy_loop import ReplayPolicy, run_policy
            actions=[{"tool":"write_file","arguments":{"path":"output/"+k,"content":v}} for k,v in outputs.items()]+[{"finish":True}]
            if mode=="replay_no_write": actions=[{"finish":True}]
            if mode=="replay_bad_path": actions=[{"tool":"read_file","arguments":{"path":"../private/inputs/policy.json"}}]
            if mode=="replay_budget": actions=[{"tool":"list_files","arguments":{}}]*8
            if mode=="replay_recover": actions=[{"tool":"read_file","arguments":{"path":"inputs/missing.csv"}},*actions]
            result=await run_policy(ReplayPolicy(actions,delay=.1 if mode=="replay_timeout" else 0),tools,
                                    max_steps=3 if mode=="replay_budget" else 12,step_timeout=.02 if mode=="replay_timeout" else 2)
            save(run/"policy_loop.json",result)
            return result["termination_reason"]
        for name,text in outputs.items():
            tools.call("write_file",{"path":"output/"+name,"content":text})
        output=run/"workspace/output"
        data_file=output/{"receivables":"ledger.csv","meeting":"schedule.csv","documents":"decisions.csv"}[self.spec["family"]]
        if mode in ("wrong_object","wrong_amount","partial"):
            data=list(csv.reader(io.StringIO(data_file.read_text(encoding="utf-8"))))
            if mode=="wrong_object": data[1][0]="NON_TARGET"
            elif mode=="partial": data.pop()
            elif self.spec["family"]=="receivables": data[1][4]="999999.00"
            elif self.spec["family"]=="meeting": data[1][3]=data[1][2]
            else: data[1][1]="rejected" if data[1][1]=="approved" else "approved"
            data_file.write_text(csv_text(data[0],data[1:]),encoding="utf-8")
        elif mode=="missing_artifact": data_file.unlink()
        elif mode=="corrupt_artifact": (output/"summary.json").write_bytes(b"not JSON")
        elif mode=="allowed_format":
            (output/"report.md").write_text("\n\n"+outputs["report.md"]+"\n",encoding="utf-8")
        elif mode=="tamper_input":
            (run/"workspace/inputs/policy.json").write_text("{}",encoding="utf-8")
        elif mode in ("wrong_citation","fabricated_quote"):
            if self.spec["family"]!="documents":raise ValueError("Citation fault injection requires documents task")
            path=output/"evidence.json"
            evidence=read_json(path)
            first=next(iter(evidence.values()))
            first["document_id" if mode=="wrong_citation" else "quote"]="FABRICATED"
            save(path,evidence)
        elif mode=="wrong_report":
            (output/"report.md").write_text("# 已完成\n所有请求均已批准。\n",encoding="utf-8")
        elif mode=="tool_error": tools.call("unknown_tool",{})
        return "scripted_path_finished"

    def evaluate(self,run,mode):
        self.check_private(run)
        if mode=="verifier_error":
            return {"evaluation_status":"error","task_passed":None,"score":None,"reward":None,
                    "error_category":"verifier_infrastructure","check_results":[],"failure_reason":"explicit deterministic verifier fault injection"}
        files=file_snapshot(run/"workspace")
        checks=[{"name":"visible_input_unchanged_"+name,"passed":files.get(name)==value} for name,value in self.manifest["asset_sha256"].items()]
        input_root=run/"private/inputs"
        inputs={p.relative_to(input_root).as_posix():p.read_text(encoding="utf-8") for p in input_root.rglob("*") if p.is_file()}
        checks+=verify(self.spec["family"],inputs,run/"workspace/output")
        return {"evaluation_status":"completed","task_passed":all(c["passed"] for c in checks),"score":None,
                "reward":None,"error_category":None,"check_results":checks}
