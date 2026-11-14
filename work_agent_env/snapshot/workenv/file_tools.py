"""Bounded file capabilities for local diagnostics; not an OS sandbox."""
from pathlib import Path
from dataclasses import dataclass
from copy import deepcopy
import errno
import uuid

from workenv.core import contained, now, no_links, ResourceError
from workenv.tool_contract import validate_arguments
from workenv.atomic_io import replace_atomic


class ToolBudgetError(ValueError):
    """An action exceeds the declared local tool budget."""


@dataclass(frozen=True)
class FileLimits:
    max_file_bytes:int=512*1024
    max_output_bytes:int=4*1024*1024
    max_output_files:int=64

    def __post_init__(self):
        if any(type(v) is not int or v<=0 for v in (self.max_file_bytes,self.max_output_bytes,self.max_output_files)):
            raise ValueError("File limits must be positive integers")


RESOURCE_ERRNOS={errno.EACCES,errno.EPERM,errno.ENOSPC,errno.EIO,errno.EROFS,errno.EMFILE,errno.ENFILE}
if hasattr(errno,"EDQUOT"):RESOURCE_ERRNOS.add(errno.EDQUOT)


class FileTools:
    def __init__(self, workspace: Path, trace: list, *, limits:FileLimits|None=None):
        self.workspace,self.trace=workspace,trace
        self.limits=limits or FileLimits()

    def _check_output_budget(self,path,data):
        output=contained(self.workspace,"output")
        files=[]
        if output.exists():
            for item in output.rglob("*"):
                no_links(item,output)
                if item.is_file():files.append(item)
        if path.exists() and not path.is_file():raise ValueError("Output target is not a regular file")
        old_size=path.stat().st_size if path.is_file() else 0
        count=len(files)+(0 if path.is_file() else 1)
        total=sum(p.stat().st_size for p in files)-old_size+len(data)
        if count>self.limits.max_output_files:raise ToolBudgetError("Output file count budget exceeded")
        if total>self.limits.max_output_bytes:raise ToolBudgetError("Total output byte budget exceeded")

    def _write(self,path,data):
        self._check_output_budget(path,data)
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name(".write_"+uuid.uuid4().hex+".tmp")
        try:
            with temporary.open("xb") as stream:stream.write(data)
            replace_atomic(temporary,path)
        finally:
            if temporary.exists():temporary.unlink()

    def call(self, name: str, arguments: dict):
        event={"type":"tool_call","tool":name,"arguments":deepcopy(arguments),"started_at":now(),"is_model_output":False}
        self.trace.append(event)
        try:
            validate_arguments(name,arguments,"local_files")
            if name=="list_files":
                from workenv.core import file_snapshot
                result=sorted(file_snapshot(self.workspace))
            elif name in ("read_file","write_file"):
                if not isinstance(arguments["path"],str):raise ValueError("Path must be a string")
                relative=arguments["path"].replace("\\","/")
                if ":" in relative or any(part.startswith(".") for part in relative.split("/")):
                    raise ValueError("Hidden paths, device syntax and parent components are forbidden")
                path=contained(self.workspace,relative)
                if relative!="task.md" and not relative.startswith(("inputs/","output/")):
                    raise ValueError("File capability does not include this path")
                if name=="read_file":
                    if path.stat().st_size>self.limits.max_file_bytes:
                        raise ToolBudgetError("Read budget exceeded")
                    result=path.read_text(encoding="utf-8")
                else:
                    if not relative.startswith("output/"):
                        raise ValueError("Inputs are read-only")
                    data=arguments["content"]
                    if not isinstance(data,str):raise ValueError("Content must be a string")
                    encoded=data.encode("utf-8")
                    if len(encoded)>self.limits.max_file_bytes:raise ToolBudgetError("Write budget exceeded")
                    self._write(path,encoded)
                    result={"written":relative,"bytes":len(data.encode())}
            else:
                raise ValueError("Unknown file tool")
            event.update(ended_at=now(),response=deepcopy(result))
            return result
        except OSError as exc:
            if exc.errno in RESOURCE_ERRNOS:
                error=ResourceError("File resource unavailable: "+str(exc))
                event.update(ended_at=now(),error=type(error).__name__+": "+str(error))
                raise error from exc
            event.update(ended_at=now(),error=type(exc).__name__+": "+str(exc))
            raise
        except Exception as exc:
            event.update(ended_at=now(),error=type(exc).__name__+": "+str(exc))
            raise
