"""Synthetic policy revision review with independently checked decisions and citations."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import random
import re
import sqlite3

from workenv.taskpacks import csv_text


def document_world(seed):
    rng=random.Random(seed)
    old_limit=rng.randint(3,5)
    new_limit=old_limit+4
    specifications=[("GLOBAL-OLD","approved","2026-08-01","all",old_limit,"team_lead"),
                    ("GLOBAL-CURRENT","approved","2026-10-01","all",new_limit,"office_manager"),
                    ("GLOBAL-FUTURE","approved","2026-11-01","all",30,"future_owner"),
                    ("GLOBAL-DRAFT","draft","2026-10-10","all",60,"draft_owner"),
                    ("OPS-OLD","approved","2026-08-01","Operations",2,"ops_lead"),
                    ("OPS-CURRENT","approved","2026-10-01","Operations",6,"ops_manager")]
    inputs={}
    for suffix,status,effective,department,maximum,approver in specifications:
        identifier=f"DOC-{seed}-{suffix}"
        inputs[f"policies/{suffix.lower()}.md"]=(f"# {identifier}\nStatus: {status}\nEffective: {effective}\n"
            f"Department: {department}\nMaximum days: {maximum}\nApprover: {approver}\n\n"
            "本制度规定合成设备借用时限，requested_days 不超过 Maximum days 时 approved，否则 rejected。\n")
    requests=[]
    for i in range(12):
        requests.append([f"REQ-{seed}-{i:03}",("Research","Operations","Support")[i%3],(2,old_limit+1,new_limit+1,5)[i%4]])
    rng.shuffle(requests)
    inputs["requests.csv"]=csv_text(["request_id","department","requested_days"],requests)
    return inputs


def reference_documents(inputs,*,alternative=False):
    policy=json.loads(inputs["policy.json"])
    docs=[]
    for path,text in inputs.items():
        if not path.startswith("policies/"):continue
        lines=text.splitlines()
        fields=dict(line.split(": ",1) for line in lines[1:6])
        docs.append({**fields,"id":lines[0][2:],"path":path,"text":text})
    decisions=[]
    citations={}
    for request in csv.DictReader(io.StringIO(inputs["requests.csv"])):
        candidates=[d for d in docs if d["Status"]=="approved" and d["Effective"]<=policy["as_of"]
                    and d["Department"] in ("all",request["department"])]
        chosen=max(candidates,key=lambda d:(d["Department"]!="all",d["Effective"],d["id"]))
        decision="approved" if int(request["requested_days"])<=int(chosen["Maximum days"]) else "rejected"
        decisions.append([request["request_id"],decision,chosen["Approver"],chosen["id"]])
        citations[request["request_id"]]={"document_id":chosen["id"],"path":"inputs/"+chosen["path"],
            "line_start":1,"line_end":6,"quote":"\n".join(chosen["text"].splitlines()[:6])}
    decisions.sort(reverse=alternative)
    approved=sum(r[1]=="approved" for r in decisions)
    summary={"request_count":len(decisions),"approved_count":approved,"rejected_count":len(decisions)-approved,"as_of":policy["as_of"]}
    used=sorted({c["path"] for c in citations.values()})
    report=(f"# 制度版本核对报告\n\n截至 {policy['as_of']}，依据 requests.csv 与以下有效已批准制度：\n\n"
            +"\n".join("- "+p for p in used)+f"\n\n| decision | count |\n| --- | --- |\n| approved | {approved} |\n| rejected | {len(decisions)-approved} |\n")
    return {"decisions.csv":csv_text(["request_id","decision","approver","policy_id"],decisions),
            "evidence.json":json.dumps(citations,ensure_ascii=False,indent=2)+"\n",
            "summary.json":json.dumps(summary,indent=2)+"\n","report.md":report}


def verify_documents(inputs:dict[str,str],output:Path):
    checks=[]
    def check(name,value):checks.append({"name":name,"passed":bool(value)})
    for name in ("decisions.csv","evidence.json","summary.json","report.md"):
        check("exists_"+name,(output/name).is_file())
    if not all(c["passed"] for c in checks):return checks
    if any((output/name).stat().st_size>512*1024 for name in ("decisions.csv","evidence.json","summary.json","report.md")):
        check("artifact_budget",False)
        return checks
    # Independent parser + SQL selection; never calls the reference implementation.
    as_of=json.loads(inputs["policy.json"])["as_of"]
    docs={}
    records=[]
    for path,text in inputs.items():
        if not path.startswith("policies/"):continue
        identifier=re.search(r"^# (DOC-\S+)$",text,re.M).group(1)
        fields={key:re.search(r"^"+re.escape(key)+r": (.+)$",text,re.M).group(1) for key in ("Status","Effective","Department","Maximum days","Approver")}
        records.append((identifier,path,fields["Status"],fields["Effective"],fields["Department"],int(fields["Maximum days"]),fields["Approver"]))
        docs[identifier]=(path,text)
    requests=list(csv.DictReader(io.StringIO(inputs["requests.csv"])))
    truth={}
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE policy(id TEXT,path TEXT,status TEXT,effective TEXT,department TEXT,days INTEGER,approver TEXT)")
        conn.executemany("INSERT INTO policy VALUES (?,?,?,?,?,?,?)",records)
        for req in requests:
            selected=conn.execute("SELECT id,path,days,approver FROM policy WHERE status='approved' AND effective<=? AND department IN ('all',?) ORDER BY (department<>'all') DESC,effective DESC,id DESC LIMIT 1",(as_of,req["department"])).fetchone()
            if selected is None:raise ValueError("Frozen world has no applicable approved policy")
            identifier,path,days,approver=selected
            truth[req["request_id"]]={"request_id":req["request_id"],"decision":"approved" if int(req["requested_days"])<=days else "rejected","approver":approver,"policy_id":identifier}
    try:
        with (output/"decisions.csv").open(encoding="utf-8",newline="") as stream:
            reader=csv.DictReader(stream)
            check("decision_columns",reader.fieldnames==["request_id","decision","approver","policy_id"])
            actual=list(reader)
        check("exact_request_set",len(actual)==len(truth) and {r.get("request_id") for r in actual}==set(truth))
        for row in actual:check("decision_"+str(row.get("request_id")),row==truth.get(row.get("request_id")))
        evidence=json.loads((output/"evidence.json").read_text(encoding="utf-8"))
        check("exact_citation_set",isinstance(evidence,dict) and set(evidence)==set(truth))
        for request_id,expected in truth.items():
            citation=evidence.get(request_id,{})
            path,text=docs[expected["policy_id"]]
            begin,end=citation.get("line_start"),citation.get("line_end")
            valid_range=type(begin) is int and type(end) is int and begin==1 and 6<=end<=len(text.splitlines())
            check("source_"+request_id,citation.get("document_id")==expected["policy_id"] and citation.get("path")=="inputs/"+path)
            check("quote_"+request_id,valid_range and citation.get("quote")=="\n".join(text.splitlines()[begin-1:end]))
        approved=sum(r["decision"]=="approved" for r in truth.values())
        summary=json.loads((output/"summary.json").read_text(encoding="utf-8"))
        expected_summary={"request_count":len(truth),"approved_count":approved,"rejected_count":len(truth)-approved,"as_of":as_of}
        check("summary",summary==expected_summary and all(type(summary[k]) is int for k in ("request_count","approved_count","rejected_count")))
        report=(output/"report.md").read_text(encoding="utf-8")
        check("report_scope",as_of in report and "requests.csv" in report)
        check("report_counts",f"| approved | {approved} |" in report and f"| rejected | {len(truth)-approved} |" in report)
        check("report_sources",all("inputs/"+docs[r["policy_id"]][0] in report for r in truth.values()))
    except (ValueError,TypeError,KeyError,AttributeError,UnicodeError,csv.Error):
        check("artifact_format",False)
    return checks
