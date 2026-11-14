"""Reproducible synthetic office worlds and provenance-preserving task variants."""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import random

from workenv.core import ROOT, contained, digest, read_json, save

VERSION = "local-office-v0.1"


def csv_text(header, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def world(family: str, seed: int) -> dict[str,str]:
    rng = random.Random(seed)
    if family == "tickets":
        from workenv.ticket_worlds import ticket_world
        return ticket_world(seed)
    if family == "documents":
        from workenv.document_tasks import document_world
        return document_world(seed)
    if family == "receivables":
        invoices, payments = [], []
        for i in range(24):
            identifier = f"INV-{seed}-{i:03}"
            day = date(2026,9,1) + timedelta(days=rng.randrange(40))
            cents = rng.randrange(1000, 90000)
            status = "cancelled" if i%7==0 else "approved"
            invoices.append([identifier, ("Research","Operations","Support")[i%3],day.isoformat(),
                             (day+timedelta(days=12)).isoformat(),status,f"{cents/100:.2f}"])
            for fraction,offset in ((2,5),(4,27)):
                paid = cents//fraction
                payments.append([identifier,(day+timedelta(days=offset)).isoformat(),f"{paid/100:.2f}"])
        rng.shuffle(invoices)
        rng.shuffle(payments)
        return {"invoices.csv":csv_text(["invoice_id","department","issue_date","due_date","status","amount"],invoices),
                "payments.csv":csv_text(["invoice_id","payment_date","amount"],payments)}
    if family == "meeting":
        base = datetime(2026,9,21,9,0) + timedelta(days=seed%5)
        people = [f"Person-{seed}-{i}" for i in range(4)]
        requests=[]
        for i in range(4):
            attendees = ";".join(sorted((people[i%4],people[(i+1)%4])))
            requests.append([f"MTG-{seed}-{i}",attendees,30 if i%2 else 60,base.isoformat(timespec="minutes"),
                             (base+timedelta(hours=8)).isoformat(timespec="minutes")])
        availability=[[person,base.isoformat(timespec="minutes"),(base+timedelta(hours=8)).isoformat(timespec="minutes")] for person in people]
        busy=[[people[0],(base+timedelta(hours=1)).isoformat(timespec="minutes"),(base+timedelta(hours=2)).isoformat(timespec="minutes")],
              [people[2],(base+timedelta(hours=3)).isoformat(timespec="minutes"),(base+timedelta(hours=4)).isoformat(timespec="minutes")]]
        rng.shuffle(requests)
        return {"requests.csv":csv_text(["meeting_id","attendees","duration_minutes","earliest","latest"],requests),
                "availability.csv":csv_text(["person","start","end"],availability),
                "busy.csv":csv_text(["person","start","end"],busy)}
    raise ValueError("Unsupported family")


def pack(family: str, seed: int, variant="base", split="dev") -> dict:
    if family not in ("receivables","meeting","documents","tickets") or variant not in ("base","changed_constraint"):
        raise ValueError("Unsupported task family or variant")
    if split not in ("dev","train","validation","test") or type(seed) is not int or not 0<=seed<=100000:
        raise ValueError("Invalid seed/split")
    inputs=world(family,seed)
    if family=="receivables":
        policy={"period_start":"2026-09-01","period_end":"2026-09-30",
                "payment_cutoff":"2026-10-05" if variant=="base" else "2026-09-20",
                "status":"approved","currency":"synthetic_units"}
        instruction="""核对 inputs/invoices.csv 和 inputs/payments.csv，依照 inputs/policy.json 中的期间与付款截至日处理 approved 发票。取消的、期间外的发票不计入，付款日期晚于截至日的不计入已付金额；到期日严格早于截至日且仍未结清才计入逾期。金额均为合成单位。\n在 output/ledger.csv 交付 invoice_id,department,invoiced,paid,outstanding,overdue 六列；金额保留两位小数，overdue 使用 true/false，每张合格发票恰好一行。output/summary.json 给出 invoice_count,total_invoiced,total_paid,total_outstanding,overdue_count,payment_cutoff。另在 output/report.md 用简短文字说明范围、截至日及资料来源，至少引用 invoices.csv 和 payments.csv。请自行核对数据并交付真实文件。"""
        workflow="invoice_payment_reconciliation"
    elif family=="meeting":
        policy={"timezone":"Asia/Shanghai","slot_minutes":30,"not_before_hour":9 if variant=="base" else 13}
        instruction="""根据 inputs/requests.csv、availability.csv 和 busy.csv 安排所有会议，并遵守 inputs/policy.json 的时区、时间粒度和最早小时。每个会议必须在自身 earliest/latest 范围内、每位参会者的可用时段内，时长与请求一致；不能重叠人员已有占用，也不能让参会者同时参加两个新会议。\n在 output/schedule.csv 交付 meeting_id,attendees,start,end 四列，attendees 用分号分隔，时间为当地 YYYY-MM-DDTHH:MM。每个请求恰好一行，不要求某个唯一安排或固定顺序。output/summary.json 包含 scheduled_count,timezone。另在 output/report.md 说明范围、规则与来源，至少引用 requests.csv、availability.csv、busy.csv。"""
        workflow="multi_person_meeting_scheduling"
    elif family=="documents":
        policy={"as_of":"2026-10-15" if variant=="base" else "2026-09-15"}
        instruction="""读取 inputs/requests.csv 与 inputs/policies/ 中的制度 Markdown，按 inputs/policy.json 的 as_of 日期核对合成设备借用请求。只考虑 Status=approved 且 Effective 不晚于 as_of 的制度；适用部门为请求 department 或 all。部门专用制度优先于 all，在同一优先级选 Effective 最新者，如同日则选文档 ID 字典序最大者。requested_days 不超过 Maximum days 时 approved，否则 rejected，approver 取所选制度，不能使用 draft 或未来版本。\n交付 output/decisions.csv，列为 request_id,decision,approver,policy_id，每请求恰好一行。output/evidence.json 以 request_id 为键，值为 document_id,path,line_start,line_end,quote；path 为 inputs/policies/...，引用从第 1 行开始且至少到第 6 行，须包含身份与规则字段，quote 为对应原文行以换行连接，不带尾换行。output/summary.json 包含 request_count,approved_count,rejected_count,as_of。output/report.md 说明截至日、requests.csv 和所有采用的制度路径，并用 Markdown 行 | approved | 数量 | 和 | rejected | 数量 | 报告计数。资料全为合成示例，并非真实制度。"""
        workflow="policy_revision_review_with_citations"
    else:
        from workenv.ticket_worlds import ticket_policy,INSTRUCTION
        policy=ticket_policy(variant)
        instruction=INSTRUCTION
        workflow="ticket_routing_with_approval"
    inputs["policy.json"]=json.dumps(policy,sort_keys=True,indent=2)+"\n"
    world_assets=world(family,seed)
    world_hash=hashlib.sha256(json.dumps(world_assets,sort_keys=True).encode()).hexdigest()
    content_hash=hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()
    task_id=f"local-{family}-{seed}-{variant}"
    return {"task_id":task_id,"family":family,"workflow_id":workflow,"world_id":f"{family}-{seed}",
            "leakage_group_id":f"{workflow}:{world_hash}","split":split,"variant":variant,"seed":seed,
            "task_version":"local-tickets-v0.1" if family=="tickets" else "local-documents-v0.1" if family=="documents" else VERSION,
            "runtime_adapter":"local_tickets" if family=="tickets" else "local_files","inputs":inputs,"instruction":instruction,
            "world_content_sha256":world_hash,"content_sha256":content_hash,"source":"project_synthetic",
            "license":"project-generated synthetic fixtures; not externally published",
            "evaluation_design":"same workflow, new synthetic worlds; not unseen-workflow generalization",
            "split_status":"construction_candidate_not_frozen"}


def write_pack(task: dict) -> dict:
    registry_path=ROOT/"tasks/registry.json"
    registry=read_json(registry_path)
    existing=registry["tasks"]
    for other in existing:
        if (other.get("leakage_group_id")==task["leakage_group_id"] or
            other.get("world_content_sha256")==task["world_content_sha256"] or
            other.get("content_sha256")==task["content_sha256"]) and other["split"]!=task["split"]:
            raise ValueError("World/variant leakage across splits")
    relative="tasks/local/"+task["task_id"]
    destination=contained(ROOT,relative)
    manifest={k:v for k,v in task.items() if k not in ("inputs","instruction")}
    expected={"task.md":task["instruction"]+"\n",**{"inputs/"+k:v for k,v in task["inputs"].items()}}
    manifest["asset_sha256"]={k:hashlib.sha256(v.encode()).hexdigest() for k,v in expected.items()}
    if destination.exists():
        if read_json(destination/"task.json")!=manifest or any(digest(contained(destination,k))!=v for k,v in manifest["asset_sha256"].items()):
            raise ValueError("Existing task pack differs; create a new version instead of overwriting")
    else:
        destination.mkdir(parents=True)
        for relative_path,text in expected.items():
            target=contained(destination,relative_path)
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(text.encode())
        save(destination/"task.json",manifest)
    entry={**manifest,"source_path":relative,"resource_group":"independent_sqlite" if task["family"]=="tickets" else "independent_files","agent_runtime_status":"not_run"}
    matches=[i for i,t in enumerate(existing) if t["task_id"]==task["task_id"]]
    if matches:
        if len(matches)!=1 or existing[matches[0]]!=entry:
            raise ValueError("Registry conflicts with immutable task pack")
    else:
        existing.append(entry)
    registry["contract_version"]="0.2"
    save(registry_path,registry)
    return entry


def audit_registry() -> dict:
    from workenv.catalog import audit_registry as audit
    return audit()
