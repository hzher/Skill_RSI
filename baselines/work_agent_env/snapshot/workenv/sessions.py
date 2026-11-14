"""Caller-driven file environment lifecycle; never generates an answer or calls a model."""
from contextlib import ExitStack
import json
import hashlib
import time

from workenv.core import load_task, now, read_json, run_lock, save,partial_snapshot,ResourceError
from workenv.file_adapter import FileTaskAdapter
from workenv.file_tools import FileTools,FileLimits,ToolBudgetError
from workenv.tool_contract import public_schemas


# Compatibility snapshot; never returned by opened() or used for dispatch.
TOOL_SCHEMAS=public_schemas("local_files")


def _persist(path,value):
    try:save(path,value)
    except OSError as exc:
        raise ResourceError("Session evidence persistence failed: "+str(exc)) from exc


class FileSession:
    def __init__(self,task_id,*,run_id=None,max_tool_calls=50):
        if type(max_tool_calls) is not int or not 1<=max_tool_calls<=200:
            raise ValueError("max_tool_calls must be in 1..200")
        spec=load_task(task_id)
        if spec["runtime_adapter"] not in ("local_files","local_tickets"):
            raise ValueError("Interactive sessions require an isolated local adapter")
        from workenv.runner import adapter_for
        self.adapter=adapter_for(spec)
        self.trace=[]
        self.stack=ExitStack()
        self.finished=False
        self.calls=0
        self.max_calls=max_tool_calls
        self.request_bytes=0
        self.max_request_bytes=8*1024*1024
        self.started=time.monotonic()
        self.run=None
        try:
            self.stack.enter_context(self.adapter.guard())
            self.run=self.adapter.create("external_tools",run_id)
            self.stack.enter_context(run_lock(self.run))
            metadata=read_json(self.run/"run.json")
            metadata.update(execution_kind="external_tool_session",execution_mode="external_tools",
                            action_provenance="external_unattested",real_model_calls=None,server_model_calls=0,
                            max_tool_calls=max_tool_calls,training_eligible=False,
                            resource_limits={"max_file_bytes":FileLimits().max_file_bytes,
                                "max_output_bytes":FileLimits().max_output_bytes,"max_output_files":FileLimits().max_output_files,
                                "max_total_tool_request_bytes":self.max_request_bytes})
            _persist(self.run/"run.json",metadata)
            initial=self.adapter.reset(self.run,archive=False)
            _persist(self.run/"initial_state.json",initial)
            self.tools=self.adapter.tools(self.run,self.trace)
            _persist(self.run/"trace.json",self.trace)
        except Exception as exc:
            if self.run is not None:
                metadata=read_json(self.run/"run.json")
                metadata.update(execution_status="error",evaluation_status="not_completed",task_passed=None,
                                score=None,reward=None,termination_reason="initialization_error",error_category="initialization",error=type(exc).__name__+": "+str(exc))
                self._save_end(metadata)
            self.stack.close()
            raise

    def opened(self):
        return {"protocol":"file-session-v0.1","run_id":self.run.name,"task_id":self.adapter.spec["task_id"],
                "instruction":(self.run/"workspace/task.md").read_text(encoding="utf-8"),
                "files":sorted(self.adapter.snapshot(self.run)["files"]),"tools":public_schemas(self.adapter.spec["runtime_adapter"]),
                "execution_kind":"external_tool_session","model_id":None,"training_eligible":False,
                "max_tool_calls":self.max_calls,"resource_limits":read_json(self.run/"run.json")["resource_limits"],"is_os_sandbox":False}

    def call(self,name,arguments):
        if self.finished:raise ValueError("Session already closed")
        if self.calls>=self.max_calls:
            self.finish("step_budget_exhausted")
            raise ValueError("Tool budget exhausted; session independently evaluated and closed")
        self.calls+=1
        try:
            encoded=json.dumps({"tool":name,"arguments":arguments},allow_nan=False,ensure_ascii=False).encode("utf-8")
            if len(encoded)>1024*1024:raise ToolBudgetError("Tool request exceeds 1 MiB")
            if self.request_bytes+len(encoded)>self.max_request_bytes:raise ToolBudgetError("Total tool request byte budget exceeded")
        except (ValueError,TypeError) as exc:
            self.trace.append({"type":"tool_rejected","tool":name if isinstance(name,str) else None,
                "argument_capture":"omitted","request_sha256":hashlib.sha256(encoded).hexdigest() if 'encoded' in locals() else None,
                "error":type(exc).__name__+": "+str(exc),"at":now(),"is_model_output":None,"action_provenance":"external_unattested"})
            _persist(self.run/"trace.json",self.trace)
            raise
        self.request_bytes+=len(encoded)
        try:
            return self.tools.call(name,arguments)
        finally:
            if self.trace:
                self.trace[-1].update(is_model_output=None,action_provenance="external_unattested")
            _persist(self.run/"trace.json",self.trace)

    def status(self):
        metadata=read_json(self.run/"run.json")
        return {"run_id":self.run.name,"closed":self.finished,"tool_calls":self.calls,
                "tool_calls_remaining":max(0,self.max_calls-self.calls),
                "execution_status":metadata["execution_status"],"evaluation_status":metadata["evaluation_status"],
                "task_passed":metadata["task_passed"],"termination_reason":metadata["termination_reason"]}

    def finish(self,reason="caller_finished"):
        if self.finished:raise ValueError("Session already closed")
        metadata=read_json(self.run/"run.json")
        try:
            final=self.adapter.snapshot(self.run)
            _persist(self.run/"final_state.json",final)
            result=self.adapter.evaluate(self.run,"external_tools")
            _persist(self.run/"evaluation.json",result)
            metadata.update(result,execution_status="completed",termination_reason=reason,check_results="evaluation.json")
        except Exception as exc:
            metadata.update(execution_status="error",evaluation_status="error",task_passed=None,
                            score=None,reward=None,error_category="verifier_infrastructure",
                            termination_reason="evaluation_error",error=type(exc).__name__+": "+str(exc))
        finally:
            self._save_end(metadata)
        return self.status()

    def _save_end(self,metadata):
        try:
            try:final=self.adapter.snapshot(self.run)
            except Exception as exc:
                final=partial_snapshot(self.run,exc)
                metadata.update(execution_status="error",evaluation_status="error",task_passed=None,score=None,reward=None,
                                final_state_capture_error=type(exc).__name__+": "+str(exc),error_category="resource_infrastructure")
            _persist(self.run/"final_state.json",final)
            metadata["artifact_refs"]=["workspace/"+name for name in final["files"] if name.startswith("output/")]
            if hasattr(self.adapter,"resource_artifacts"):metadata["resource_artifacts"]=self.adapter.resource_artifacts(self.run,final)
            metadata.update(end_time=now(),latency=time.monotonic()-self.started,tool_calls=self.calls,tool_request_bytes=self.request_bytes)
            _persist(self.run/"trace.json",self.trace)
            _persist(self.run/"run.json",metadata)
        finally:
            self.finished=True
            self.stack.close()

    def close(self,reason="client_disconnected"):
        if self.finished:return
        metadata=read_json(self.run/"run.json")
        metadata.update(execution_status="error",evaluation_status="not_completed",task_passed=None,
                        score=None,reward=None,termination_reason=reason,
                        error_category="resource_infrastructure" if reason in ("resource_error","replay_resource_error") else "external_session_interrupted")
        self._save_end(metadata)


class SessionProtocol:
    def __init__(self,*,max_tool_calls=50):
        self.session=None
        self.seen=set()
        self.max_tool_calls=max_tool_calls

    def handle(self,request):
        identifier=None
        try:
            if not isinstance(request,dict):raise ValueError("Request must be an object")
            identifier=request.get("id")
            if not isinstance(identifier,str) or not identifier or len(identifier)>100:
                raise ValueError("Request id must be a nonempty string (max 100 characters)")
            if identifier in self.seen:raise ValueError("Duplicate request id; action was not repeated")
            if len(self.seen)>=1000:raise ValueError("Request budget exhausted")
            self.seen.add(identifier)
            operation=request.get("op")
            if operation=="open":
                if self.session is not None:raise ValueError("A process owns at most one session")
                self.session=FileSession(request["task_id"],run_id=request.get("run_id"),max_tool_calls=self.max_tool_calls)
                result=self.session.opened()
            else:
                if self.session is None:raise ValueError("Open a session first")
                if operation=="tool":result=self.session.call(request["name"],request.get("arguments",{}))
                elif operation=="finish":result=self.session.finish()
                elif operation=="status":result=self.session.status()
                elif operation=="close":
                    self.session.close("caller_closed_without_finish")
                    result=self.session.status()
                else:raise ValueError("Unknown protocol operation")
            return {"id":identifier,"ok":True,"result":result}
        except ResourceError as exc:
            if self.session is not None:self.session.close("resource_error")
            return {"id":identifier,"ok":False,"error":{"kind":"resource_infrastructure","message":str(exc)},
                    "run_id":self.session.run.name if self.session is not None else None}
        except (ValueError,OSError,KeyError,TypeError,AttributeError,UnicodeError) as exc:
            return {"id":identifier,"ok":False,"error":{"kind":type(exc).__name__,"message":str(exc)}}

    def close(self):
        if self.session is not None:self.session.close()
