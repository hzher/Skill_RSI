"""Reference paths and independently implemented checks for synthetic office work."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import io
import json
from pathlib import Path
import sqlite3

from workenv.taskpacks import csv_text


def rows(inputs: dict[str,str], name: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(inputs[name])))


def cents(text: str) -> int:
    value=Decimal(str(text))
    if not value.is_finite() or value*100 != (value*100).to_integral_value():
        raise ValueError("Money must be finite with at most two decimal places")
    return int(value*100)


def money(value: int) -> str:
    return f"{Decimal(value)/100:.2f}"


def reference_receivables(inputs: dict[str,str]) -> dict[str,str]:
    policy=json.loads(inputs["policy.json"])
    payments={}
    for payment in rows(inputs,"payments.csv"):
        if payment["payment_date"]<=policy["payment_cutoff"]:
            key=payment["invoice_id"]
            payments[key]=payments.get(key,0)+cents(payment["amount"])
    ledger=[]
    for invoice in rows(inputs,"invoices.csv"):
        if invoice["status"]!=policy["status"] or not policy["period_start"]<=invoice["issue_date"]<=policy["period_end"]:
            continue
        total=cents(invoice["amount"])
        paid=payments.get(invoice["invoice_id"],0)
        remaining=total-paid
        overdue=invoice["due_date"]<policy["payment_cutoff"] and remaining>0
        ledger.append([invoice["invoice_id"],invoice["department"],money(total),money(paid),money(remaining),str(overdue).lower()])
    ledger.sort()
    summary={"invoice_count":len(ledger),"total_invoiced":money(sum(cents(r[2]) for r in ledger)),
             "total_paid":money(sum(cents(r[3]) for r in ledger)),"total_outstanding":money(sum(cents(r[4]) for r in ledger)),
             "overdue_count":sum(r[5]=="true" for r in ledger),"payment_cutoff":policy["payment_cutoff"]}
    return {"ledger.csv":csv_text(["invoice_id","department","invoiced","paid","outstanding","overdue"],ledger),
            "summary.json":json.dumps(summary,ensure_ascii=False,indent=2)+"\n",
            "report.md":f"# 核对报告\n\n依据 invoices.csv、payments.csv，范围 {policy['period_start']} 至 {policy['period_end']}，付款截至 {policy['payment_cutoff']}。仅计 approved；期间外及已取消记录排除。\n\n未结金额 {summary['total_outstanding']}，逾期 {summary['overdue_count']} 张。\n"}


def overlap(a,b,c,d):
    return a<d and c<b


def reference_meeting(inputs: dict[str,str], *, reverse=False) -> dict[str,str]:
    policy=json.loads(inputs["policy.json"])
    requests=sorted(rows(inputs,"requests.csv"),key=lambda r:r["meeting_id"])
    availability=rows(inputs,"availability.csv")
    busy=rows(inputs,"busy.csv")
    result=[]

    def search(index):
        if index==len(requests):
            return True
        request=requests[index]
        people=set(request["attendees"].split(";"))
        start=datetime.fromisoformat(request["earliest"])
        stop=datetime.fromisoformat(request["latest"])
        start=max(start,start.replace(hour=policy["not_before_hour"],minute=0))
        candidates=[]
        while start+timedelta(minutes=int(request["duration_minutes"]))<=stop:
            candidates.append(start)
            start+=timedelta(minutes=policy["slot_minutes"])
        for start in (reversed(candidates) if reverse else candidates):
            end=start+timedelta(minutes=int(request["duration_minutes"]))
            if any(not any(a["person"]==person and datetime.fromisoformat(a["start"])<=start and end<=datetime.fromisoformat(a["end"]) for a in availability) for person in people):
                continue
            if any(b["person"] in people and overlap(start,end,datetime.fromisoformat(b["start"]),datetime.fromisoformat(b["end"])) for b in busy):
                continue
            if any(people & set(r[1].split(";")) and overlap(start,end,datetime.fromisoformat(r[2]),datetime.fromisoformat(r[3])) for r in result):
                continue
            result.append([request["meeting_id"],";".join(sorted(people)),start.isoformat(timespec="minutes"),end.isoformat(timespec="minutes")])
            if search(index+1):
                return True
            result.pop()
        return False
    if not search(0):
        raise ValueError("No feasible schedule found for synthetic task")
    return {"schedule.csv":csv_text(["meeting_id","attendees","start","end"],result),
            "summary.json":json.dumps({"scheduled_count":len(result),"timezone":policy["timezone"]},indent=2)+"\n",
            "report.md":f"# 会议安排\n\n依据 requests.csv、availability.csv、busy.csv，并按 {policy['timezone']} 与最早 {policy['not_before_hour']}:00 规则核对可用时间及冲突。已安排 {len(result)} 场。\n"}


def reference(family: str, inputs: dict[str,str], *, alternative=False) -> dict[str,str]:
    if family=="documents":
        from workenv.document_tasks import reference_documents
        return reference_documents(inputs,alternative=alternative)
    if family=="receivables":
        result=reference_receivables(inputs)
        if alternative:
            data=list(csv.reader(io.StringIO(result["ledger.csv"])))
            result["ledger.csv"]=csv_text(data[0],reversed(data[1:]))
        return result
    if family=="meeting":
        return reference_meeting(inputs,reverse=alternative)
    raise ValueError("Unsupported office family")


def verify(family: str, private_inputs: dict[str,str], output: Path) -> list[dict]:
    """Read frozen inputs; invoice truth uses SQL rather than the reference solver."""
    if family=="documents":
        from workenv.document_tasks import verify_documents
        return verify_documents(private_inputs,output)
    checks=[]
    def check(name, passed, detail=None):
        checks.append({"name":name,"passed":bool(passed),"detail":detail})
    policy=json.loads(private_inputs["policy.json"])
    names=("ledger.csv","summary.json","report.md") if family=="receivables" else ("schedule.csv","summary.json","report.md")
    for name in names:
        check("exists_"+name,(output/name).is_file())
    if not all((output/n).is_file() for n in names):
        return checks
    if any((output/n).stat().st_size>512*1024 for n in names):
        check("artifact_size",False)
        return checks
    try:
        summary=json.loads((output/"summary.json").read_text(encoding="utf-8"))
        if not isinstance(summary,dict):
            raise ValueError("Summary must be an object")
        if family=="receivables":
            with sqlite3.connect(":memory:") as conn:
                conn.execute("CREATE TABLE invoice(id TEXT PRIMARY KEY,department TEXT,issue TEXT,due TEXT,status TEXT,total INTEGER)")
                conn.execute("CREATE TABLE payment(id TEXT,day TEXT,amount INTEGER)")
                conn.executemany("INSERT INTO invoice VALUES (?,?,?,?,?,?)",[(r["invoice_id"],r["department"],r["issue_date"],r["due_date"],r["status"],cents(r["amount"])) for r in rows(private_inputs,"invoices.csv")])
                conn.executemany("INSERT INTO payment VALUES (?,?,?)",[(r["invoice_id"],r["payment_date"],cents(r["amount"])) for r in rows(private_inputs,"payments.csv")])
                truth=conn.execute("SELECT i.id,i.department,i.total,coalesce((SELECT sum(p.amount) FROM payment p WHERE p.id=i.id AND p.day<=?),0),i.due FROM invoice i WHERE i.status=? AND i.issue BETWEEN ? AND ? ORDER BY i.id",
                    (policy["payment_cutoff"],policy["status"],policy["period_start"],policy["period_end"])).fetchall()
            with (output/"ledger.csv").open(encoding="utf-8",newline="") as stream:
                reader=csv.DictReader(stream)
                check("ledger_header",reader.fieldnames==["invoice_id","department","invoiced","paid","outstanding","overdue"])
                actual=list(reader)
            expected={r[0]:r for r in truth}
            check("exact_invoice_set",len(actual)==len(truth) and {r.get("invoice_id") for r in actual}==set(expected))
            for r in actual:
                expected_row=expected.get(r.get("invoice_id"))
                if not expected_row:
                    check("unknown_invoice",False)
                    continue
                key,department,total,paid,due=expected_row
                check("invoice_"+key,r["department"]==department and cents(r["invoiced"])==total and cents(r["paid"])==paid and cents(r["outstanding"])==total-paid
                      and r["overdue"]==str(due<policy["payment_cutoff"] and total-paid>0).lower())
            amounts={"total_invoiced":sum(r[2] for r in truth),"total_paid":sum(r[3] for r in truth),"total_outstanding":sum(r[2]-r[3] for r in truth)}
            check("summary_keys",set(summary)=={"invoice_count","total_invoiced","total_paid","total_outstanding","overdue_count","payment_cutoff"})
            check("invoice_count",type(summary.get("invoice_count")) is int and summary["invoice_count"]==len(truth))
            for key,value in amounts.items():
                check(key,cents(summary[key])==value)
            check("overdue_count",type(summary.get("overdue_count")) is int and summary["overdue_count"]==sum(r[4]<policy["payment_cutoff"] and r[2]>r[3] for r in truth))
            check("cutoff",summary.get("payment_cutoff")==policy["payment_cutoff"])
            sources=("invoices.csv","payments.csv")
        else:
            with (output/"schedule.csv").open(encoding="utf-8",newline="") as stream:
                reader=csv.DictReader(stream)
                check("schedule_header",reader.fieldnames==["meeting_id","attendees","start","end"])
                actual=list(reader)
            requests={r["meeting_id"]:r for r in rows(private_inputs,"requests.csv")}
            availability=rows(private_inputs,"availability.csv")
            busy=rows(private_inputs,"busy.csv")
            check("exact_meeting_set",len(actual)==len(requests) and {r.get("meeting_id") for r in actual}==set(requests))
            intervals=[]
            for r in actual:
                request=requests.get(r.get("meeting_id"))
                if not request:
                    check("unknown_meeting",False)
                    continue
                begin,end=datetime.strptime(r["start"],"%Y-%m-%dT%H:%M"),datetime.strptime(r["end"],"%Y-%m-%dT%H:%M")
                people=set(r["attendees"].split(";"))
                check(r["meeting_id"]+"_attendees",people==set(request["attendees"].split(";")) and len(people)==len(r["attendees"].split(";")))
                check(r["meeting_id"]+"_duration",(end-begin).total_seconds()==int(request["duration_minutes"])*60)
                check(r["meeting_id"]+"_window",datetime.fromisoformat(request["earliest"])<=begin<end<=datetime.fromisoformat(request["latest"]))
                check(r["meeting_id"]+"_policy",begin.hour>=policy["not_before_hour"] and begin.minute%policy["slot_minutes"]==0 and end.minute%policy["slot_minutes"]==0)
                for person in people:
                    allowed=[a for a in availability if a["person"]==person and datetime.fromisoformat(a["start"])<=begin and end<=datetime.fromisoformat(a["end"])]
                    check(r["meeting_id"]+"_available_"+person,bool(allowed))
                    check(r["meeting_id"]+"_busy_"+person,not any(b["person"]==person and max(begin,datetime.fromisoformat(b["start"]))<min(end,datetime.fromisoformat(b["end"])) for b in busy))
                intervals.append((people,begin,end))
            for i,(people,begin,end) in enumerate(intervals):
                check("no_overlap_"+str(i),all(not people & other or max(begin,b)>=min(end,e) for other,b,e in intervals[i+1:]))
            check("summary",set(summary)=={"scheduled_count","timezone"} and type(summary.get("scheduled_count")) is int and summary["scheduled_count"]==len(requests) and summary["timezone"]==policy["timezone"])
            sources=("requests.csv","availability.csv","busy.csv")
        report=(output/"report.md").read_text(encoding="utf-8")
        check("report_sources",all(source in report for source in sources) and len(report.strip())>=30)
        check("report_scope",policy["payment_cutoff"] in report if family=="receivables" else policy["timezone"] in report)
    except (ValueError,TypeError,KeyError,IndexError,UnicodeError,csv.Error,InvalidOperation) as exc:
        check("artifact_format_and_values",False,type(exc).__name__+": "+str(exc))
    return checks
