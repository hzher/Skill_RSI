"""Build the current prompt's appendix using exclusively earlier accepted public QUERY traffic."""
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from unittest.mock import patch


def load_gateway(package):
    directory=(package/"scripts").resolve()
    # The sibling module is resolved from this exact frozen package, never an installed plugin.
    def load(name,path):
        spec=importlib.util.spec_from_file_location(name,path)
        module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{name:module}):
            exec(compile(path.read_bytes(),str(path),"exec"),module.__dict__)
        return module
    routes=load("routes",directory/"routes.py")
    with patch.dict(sys.modules,{"routes":routes}):
        return load("public_data_map",directory/"gateway.py")


def d(value):
    return hashlib.sha256(value.encode()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


NUMBER=re.compile(r"(?:^|\n\n)Question ([1-9][0-9]*)/([1-9][0-9]*)\s*\n")


def query_index(prompt):
    match=NUMBER.search(prompt)
    if not match:raise ValueError("Missing native sequence boundary")
    ordinal,total=map(int,match.groups())
    if not 1<=ordinal<=total:raise ValueError("Invalid native sequence")
    return ordinal,total


def material(gateway,current,out,panel,arm):
    ordinal,total=query_index(current["query"])
    notes=gateway.Notebook()
    thread=last=None
    if arm=="B" and not current["query"].startswith("NOTICE:"):
        for n in range(1,ordinal):
            task=f"{panel}-db-{n-1:03d}"
            case=out/"cases"/f"{n:03d}-{task}"
            initial=read(case/"initial-task.json")["query"]
            if query_index(initial)!=(n,total):raise ValueError("Native epoch or prompt mismatch")
            cont=read(case/"continuity.json")
            identity=read(case/"identity.json")
            if (identity["task_id"]!=task or identity["family"]!="database_exploration" or identity["canonical_index"]!=n-1
                or identity["attempt_id"]!=cont["attempt_id"] or cont["canonical_index"]!=n-1 or cont["previous_attempt_id"]!=last or not cont["thread_id"]
                or thread is not None and thread!=cont["thread_id"]):
                raise ValueError("Incomplete prior session chain")
            if initial.startswith("NOTICE:"):notes=gateway.Notebook()
            thread,last=cont["thread_id"],cont["attempt_id"]
            journal=(case/"broker.jsonl").read_bytes()
            if read(case/"artifact-manifest.json")["broker.jsonl"]["sha256"]!=hashlib.sha256(journal).hexdigest():
                raise ValueError("Changed prior public journal")
            for row in map(json.loads,journal.splitlines()):
                if row.get("type")!="tool_result" or row.get("tool")!="act" or row.get("cached_receipt") is not False or row["result"]["isError"]:continue
                action=row["arguments"]["action"]
                if action["action"]!="QUERY":continue
                payload=json.loads(row["result"]["content"][0]["text"])
                if payload.get("done") is not False or payload["action"]!=action:continue
                notes.observe(action["content"],payload["observation"]["content"],f"Q{n}/act-{row['sequence']}")
    data=notes.payload()
    return {"schema":"public-data-map-v1","arm":arm,"panel":panel,"ordinal":ordinal,"reset_notice":current["query"].startswith("NOTICE:"),
            "previous_thread":thread,"prior_attempt":last,"data":data,"appendix_sha256":d(gateway.appendix(data))}


@contextmanager
def inject(actor,root,panel,arm,record):
    gateway=load_gateway(root/"package")
    out=root/"runs"/panel/arm
    orig=actor.task_prompt
    def prompt(status):
        info=material(gateway,status,out,panel,arm)
        base=orig(status)
        addition=gateway.appendix(info["data"])
        info["native_base_sha256"]=d(base)
        info["enriched_base_sha256"]=d(base+addition)
        path=out/"map-notes"/f"{info['ordinal']:03d}.json"
        if record:
            from skillrl.vendor.common import immutable,canonical
            immutable(path,canonical(info))
        elif read(path)!=info:
            raise ValueError("Public notebook differs from prior same-thread QUERY events")
        return base+addition
    with patch.object(actor,"task_prompt",prompt):
        yield
