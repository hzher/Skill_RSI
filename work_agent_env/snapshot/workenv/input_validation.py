"""Input schema checks for the supported synthetic workflow versions; no answer generation."""
from collections import Counter
import csv
from datetime import date,datetime,timedelta
from decimal import Decimal,InvalidOperation
import io
import json
import math
import re


def require(condition,message):
    if not condition:raise ValueError("Input schema: "+message)


def table(inputs,path,columns,key=None,*,allow_empty=False):
    require(path in inputs,"missing "+path)
    reader=csv.DictReader(io.StringIO(inputs[path]),strict=True)
    require(reader.fieldnames is not None and len(reader.fieldnames)==len(set(reader.fieldnames)) and set(reader.fieldnames)==set(columns),"invalid columns in "+path)
    records=list(reader)
    require(bool(records) or allow_empty,"empty "+path)
    require(all(None not in r and all(v is not None for v in r.values()) for r in records),"malformed CSV rows in "+path)
    if key:require(all(r[key] for r in records) and len({r[key] for r in records})==len(records),"duplicate or empty "+key)
    return records


def whole(value,name,minimum=0):
    require(type(value) is int and value>=minimum,"invalid "+name)


def amount(value):
    try:
        number=Decimal(value)
        require(number.is_finite() and number>=0 and number*100==(number*100).to_integral_value(),"money must be finite, nonnegative and at most two decimal places")
    except InvalidOperation as exc:raise ValueError("Input schema: invalid money") from exc


def validate_inputs(family,inputs):
    try:
        policy=json.loads(inputs["policy.json"])
        require(isinstance(policy,dict),"policy must be an object")
        if family=="receivables":
            start=date.fromisoformat(policy["period_start"])
            end=date.fromisoformat(policy["period_end"])
            date.fromisoformat(policy["payment_cutoff"])
            require(start<=end,"reversed invoice period")
            require(policy["status"] in ("approved","cancelled"),"unsupported invoice status")
            invoices=table(inputs,"invoices.csv",("invoice_id","department","issue_date","due_date","status","amount"),"invoice_id")
            payments=table(inputs,"payments.csv",("invoice_id","payment_date","amount"),allow_empty=True)
            ids={r["invoice_id"] for r in invoices}
            for row in invoices:
                date.fromisoformat(row["issue_date"])
                date.fromisoformat(row["due_date"])
                require(row["status"] in ("approved","cancelled") and bool(row["department"]),"invalid invoice metadata")
                amount(row["amount"])
            for row in payments:
                require(row["invoice_id"] in ids,"payment refers to unknown invoice")
                date.fromisoformat(row["payment_date"])
                amount(row["amount"])
        elif family=="meeting":
            require(policy["timezone"]=="Asia/Shanghai","unsupported meeting timezone")
            whole(policy["slot_minutes"],"slot_minutes",1)
            require(policy["slot_minutes"]<=60 and 60%policy["slot_minutes"]==0,"slot must divide 60 minutes")
            whole(policy["not_before_hour"],"not_before_hour")
            require(policy["not_before_hour"]<24,"invalid earliest hour")
            requests=table(inputs,"requests.csv",("meeting_id","attendees","duration_minutes","earliest","latest"),"meeting_id")
            availability=table(inputs,"availability.csv",("person","start","end"))
            busy=table(inputs,"busy.csv",("person","start","end"),allow_empty=True)
            people={r["person"] for r in availability}
            for row in availability+busy:
                begin,end=datetime.fromisoformat(row["start"]),datetime.fromisoformat(row["end"])
                require(begin.tzinfo is None and end.tzinfo is None and begin<end,"invalid local calendar interval")
            for row in requests:
                attendees=row["attendees"].split(";")
                require(set(attendees)<=people and len(set(attendees))==len(attendees),"unknown or duplicate attendee")
                duration=int(row["duration_minutes"])
                begin,end=datetime.fromisoformat(row["earliest"]),datetime.fromisoformat(row["latest"])
                require(begin.tzinfo is None and end.tzinfo is None and duration>0 and begin+timedelta(minutes=duration)<=end,"invalid meeting window/duration")
        elif family=="documents":
            cutoff=date.fromisoformat(policy["as_of"])
            requests=table(inputs,"requests.csv",("request_id","department","requested_days"),"request_id")
            documents=[]
            for path,text in inputs.items():
                if not path.startswith("policies/"):continue
                lines=text.splitlines()
                require(len(lines)>=6 and re.fullmatch(r"# DOC-\S+",lines[0]) is not None,"invalid policy document identity")
                fields=dict(line.split(": ",1) for line in lines[1:6])
                require(set(fields)=={"Status","Effective","Department","Maximum days","Approver"},"invalid policy header")
                require(fields["Status"] in ("approved","draft","retired") and bool(fields["Approver"]),"invalid policy status/approver")
                date.fromisoformat(fields["Effective"])
                require(int(fields["Maximum days"])>=0,"negative policy limit")
                documents.append((lines[0][2:],fields))
            require(bool(documents) and len({d[0] for d in documents})==len(documents),"missing or duplicate policy documents")
            for row in requests:
                require(int(row["requested_days"])>=0 and bool(row["department"]),"invalid request")
                require(any(d["Status"]=="approved" and date.fromisoformat(d["Effective"])<=cutoff and d["Department"] in ("all",row["department"]) for _,d in documents),"no applicable approved policy")
        elif family=="tickets":
            from workenv.ticket_service import TICKET_FIELDS
            tickets=table(inputs,"tickets.csv",TICKET_FIELDS,"ticket_id")
            directory=table(inputs,"directory.csv",("person","team","active"),"person")
            start=datetime.fromisoformat(policy["as_of"])
            require(start.tzinfo is not None and start.utcoffset()==timedelta(hours=8) and policy["timezone"]=="Asia/Shanghai","invalid service timezone")
            whole(policy["max_load_per_person"],"max_load_per_person",1)
            require(isinstance(policy["routing"],dict) and isinstance(policy["sla_hours"],dict),"routing and SLA must be mappings")
            require(isinstance(policy["approval_categories"],list) and all(isinstance(v,str) for v in policy["approval_categories"]),"approval categories must be strings")
            capacity=Counter()
            for member in directory:
                require(bool(member["person"]) and bool(member["team"]) and member["active"] in ("true","false"),"invalid directory member")
                if member["active"]=="true":capacity[member["team"]]+=policy["max_load_per_person"]
            demand=Counter()
            for row in tickets:
                require(row["status"] in ("open","closed"),"initial ticket state must be open or closed")
                if row["status"]=="closed":continue
                require(row["category"] in policy["routing"] and row["priority"] in policy["sla_hours"],"unknown routing category/priority")
                hours=policy["sla_hours"][row["priority"]]
                require(type(hours) in (int,float) and math.isfinite(hours) and hours>0,"invalid SLA duration")
                require(row["team"]==row["assignee"]==row["due_at"]=="","open fixture ticket already has assignment")
                demand[policy["routing"][row["category"]]]+=1
            require(all(n<=capacity[team] for team,n in demand.items()),"insufficient active team capacity")
        else:raise ValueError("Input schema: unsupported family")
    except (KeyError,TypeError,IndexError,csv.Error,OverflowError) as exc:
        raise ValueError("Input schema: missing or malformed fields for "+family) from exc
    return {"family":family,"input_files":len(inputs),"schema_valid":True,"solvability_proven":False}
