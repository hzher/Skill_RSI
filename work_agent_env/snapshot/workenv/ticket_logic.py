"""Tool-only diagnostic path and an independent service-state verifier."""
from collections import Counter
import csv
from datetime import datetime,timedelta,timezone
import io
import json

from workenv.taskpacks import csv_text


def queue_outputs(tickets,policy):
    processed=[t for t in tickets if t["status"]!="closed"]
    processed.sort(key=lambda t:t["ticket_id"])
    counts=Counter(t["status"] for t in processed)
    summary={"processed_count":len(processed),"waiting_approval_count":counts["waiting_approval"],"assigned_count":counts["assigned"],"as_of":policy["as_of"]}
    return {"queue.csv":csv_text(["ticket_id","team","assignee","status","due_at"],[[t[f] for f in ("ticket_id","team","assignee","status","due_at")] for t in processed]),
            "summary.json":json.dumps(summary,indent=2)+"\n",
            "report.md":f"# 工单处理报告\n\n截至 {policy['as_of']}，按 policy.json 与 tickets_list、directory_list 服务资料处理。\n\n| status | count |\n| --- | --- |\n| waiting_approval | {counts['waiting_approval']} |\n| assigned | {counts['assigned']} |\n"}


def reference_tickets(tools,*,alternative=False,policy_override=None,partial=False,wrong_due=False,wrong_route=False,skip_approval=False):
    policy=json.loads(tools.call("read_file",{"path":"inputs/policy.json"}))
    if policy_override:policy.update(policy_override)
    tickets=tools.call("tickets_list",{})
    directory=tools.call("directory_list",{})
    loads=Counter(t["assignee"] for t in tickets if t["status"]!="closed" and t["assignee"])
    work=[t for t in tickets if t["status"]=="open"]
    if partial:work=work[:-1]
    for index,ticket in enumerate(work):
        tools.call("tickets_triage",{"ticket_id":ticket["ticket_id"]})
        team=policy["routing"][ticket["category"]]
        if wrong_route and index==0:team=next(t for t in sorted({p["team"] for p in directory}) if t!=team)
        people=sorted(p["person"] for p in directory if p["team"]==team and p["active"] and loads[p["person"]]<policy["max_load_per_person"])
        if not people:raise ValueError("Synthetic staffing cannot satisfy capacity")
        assignee=people[-1] if alternative else people[0]
        due=datetime.fromisoformat(policy["as_of"])+timedelta(hours=policy["sla_hours"][ticket["priority"]]+(1 if wrong_due and index==0 else 0))
        tools.call("tickets_assign",{"ticket_id":ticket["ticket_id"],"team":team,"assignee":assignee,"due_at":due.isoformat()})
        loads[assignee]+=1
        if ticket["category"] in policy["approval_categories"] and not skip_approval:
            tools.call("tickets_request_approval",{"ticket_id":ticket["ticket_id"]})
    return queue_outputs(tools.call("tickets_list",{}),policy)


def verify_tickets(inputs,state,output):
    checks=[]
    def check(name,value):checks.append({"name":name,"passed":bool(value)})
    policy=json.loads(inputs["policy.json"])
    original={r["ticket_id"]:r for r in csv.DictReader(io.StringIO(inputs["tickets.csv"]))}
    people={r["person"]:r for r in csv.DictReader(io.StringIO(inputs["directory.csv"]))}
    actual={r["ticket_id"]:r for r in state["tickets"]}
    check("exact_ticket_set",len(state["tickets"])==len(original) and set(actual)==set(original))
    directory_expected=sorted(({"person":r["person"],"team":r["team"],"active":int(r["active"]=="true")} for r in people.values()),key=lambda r:r["person"])
    check("directory_unchanged",state["directory"]==directory_expected)
    loads=Counter()
    expected_ids=[]
    expected_status={}
    for identifier,source in original.items():
        row=actual.get(identifier,{})
        if source["status"]=="closed":
            check("closed_unchanged_"+identifier,row==source)
            check("closed_has_no_events_"+identifier,not any(e["ticket_id"]==identifier for e in state["audit"]))
            continue
        expected_ids.append(identifier)
        status="waiting_approval" if source["category"] in policy["approval_categories"] else "assigned"
        expected_status[identifier]=status
        check("source_fields_"+identifier,all(row.get(k)==source[k] for k in ("requester","title","category","priority")))
        check("workflow_status_"+identifier,row.get("status")==status)
        check("routing_"+identifier,row.get("team")==policy["routing"][source["category"]])
        member=people.get(row.get("assignee"),{})
        check("assignee_"+identifier,member.get("active")=="true" and member.get("team")==row.get("team"))
        loads[row.get("assignee")]+=1
        try:
            due=datetime.fromisoformat(row.get("due_at",""))
            start=datetime.fromisoformat(policy["as_of"])
            correct_due=(due-start).total_seconds()==policy["sla_hours"][source["priority"]]*3600 and due.utcoffset()==timedelta(hours=8)
        except (ValueError,TypeError):correct_due=False
        check("deadline_"+identifier,correct_due)
        events=[e for e in state["audit"] if e["ticket_id"]==identifier]
        expected_operations=["tickets_triage","tickets_assign"]+(["tickets_request_approval"] if status=="waiting_approval" else [])
        check("audit_operations_"+identifier,[e["operation"] for e in events]==expected_operations)
        previous=source
        chain=True
        for event in events:
            try:
                before=json.loads(event["before_json"])
                after=json.loads(event["after_json"])
                chain=chain and before==previous and after["ticket_id"]==identifier
                allowed={"tickets_triage":{"status"},"tickets_assign":{"status","team","assignee","due_at"},"tickets_request_approval":{"status"}}.get(event["operation"],set())
                transitions={"tickets_triage":("open","triaged"),"tickets_assign":("triaged","assigned"),"tickets_request_approval":("assigned","waiting_approval")}
                chain=chain and set(before)==set(source) and set(after)==set(source)
                chain=chain and (before.get("status"),after.get("status"))==transitions.get(event["operation"])
                chain=chain and all(before[k]==after[k] for k in source if k not in allowed)
                previous=after
            except (ValueError,KeyError,TypeError):chain=False
        check("audit_chain_"+identifier,chain and previous==row)
    check("capacity",all(n<=policy["max_load_per_person"] for n in loads.values()))
    check("audit_targets",all(e["ticket_id"] in expected_ids for e in state["audit"]))
    check("audit_sequence",[e["seq"] for e in state["audit"]]==list(range(1,len(state["audit"])+1)))
    for name in ("queue.csv","summary.json","report.md"):check("exists_"+name,(output/name).is_file())
    if not all((output/n).is_file() for n in ("queue.csv","summary.json","report.md")):return checks
    if any((output/n).stat().st_size>512*1024 for n in ("queue.csv","summary.json","report.md")):
        check("artifact_size",False)
        return checks
    try:
        with (output/"queue.csv").open(encoding="utf-8",newline="") as stream:
            reader=csv.DictReader(stream)
            check("queue_columns",reader.fieldnames==["ticket_id","team","assignee","status","due_at"])
            queue=list(reader)
        check("queue_ids",len(queue)==len(expected_ids) and {r.get("ticket_id") for r in queue}==set(expected_ids))
        for row in queue:
            check("queue_state_"+str(row.get("ticket_id")),row=={k:actual.get(row.get("ticket_id"),{}).get(k) for k in ("ticket_id","team","assignee","status","due_at")})
        waiting=sum(s=="waiting_approval" for s in expected_status.values())
        expected_summary={"processed_count":len(expected_ids),"waiting_approval_count":waiting,"assigned_count":len(expected_ids)-waiting,"as_of":policy["as_of"]}
        summary=json.loads((output/"summary.json").read_text(encoding="utf-8"))
        check("summary",summary==expected_summary and all(type(summary[k]) is int for k in ("processed_count","waiting_approval_count","assigned_count")))
        report=(output/"report.md").read_text(encoding="utf-8")
        check("report_scope",all(t in report for t in (policy["as_of"],"policy.json","tickets_list","directory_list")))
        check("report_counts",f"| waiting_approval | {waiting} |" in report and f"| assigned | {len(expected_ids)-waiting} |" in report)
    except (ValueError,KeyError,TypeError,AttributeError,UnicodeError,csv.Error):check("artifact_format",False)
    return checks
