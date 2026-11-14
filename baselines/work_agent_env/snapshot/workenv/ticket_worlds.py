"""Synthetic support queues with routing, capacity and approval requirements."""
import json
import random


def ticket_world(seed):
    from workenv.taskpacks import csv_text
    rng=random.Random(seed)
    tickets=[]
    for i in range(9):
        category=("hardware","software","access")[i%3]
        tickets.append([f"TKT-{seed}-{i:03}",f"Requester-{seed}-{i}",f"{category} request {i}",category,
                        "high" if i%2==0 else "normal","open","","",""])
    tickets.append([f"TKT-{seed}-CLOSED",f"Requester-{seed}-old","Existing closed request","software","normal","closed","applications","applications-0","2026-10-14T09:00:00+08:00"])
    rng.shuffle(tickets)
    directory=[]
    for team in ("devices","applications","identity"):
        for i in range(2):directory.append([f"{team}-{i}",team,"true"])
    return {"tickets.csv":csv_text(["ticket_id","requester","title","category","priority","status","team","assignee","due_at"],tickets),
            "directory.csv":csv_text(["person","team","active"],directory)}


def ticket_policy(variant):
    return {"as_of":"2026-10-15T09:00:00+08:00","timezone":"Asia/Shanghai",
            "routing":{"hardware":"devices","software":"applications","access":"identity"},
            "sla_hours":{"high":4 if variant=="base" else 2,"normal":24},
            "approval_categories":["access"] if variant=="base" else ["access","hardware"],"max_load_per_person":2}


INSTRUCTION="""处理本机工单服务中所有初始 open 工单，已关闭工单保持原样。读取 inputs/policy.json 与公开人员目录，按 routing 将工单分给对应团队的 active 成员；每人本轮最多承担 max_load_per_person 个未关闭工单。先 tickets_triage，再 tickets_assign；due_at 等于 as_of 加该工单 priority 对应的 sla_hours，使用带 +08:00 的 ISO 时间。属于 approval_categories 的工单必须在分配后 tickets_request_approval，停在 waiting_approval；其余停在 assigned。不得伪造批准或修改已关闭记录。\n服务工具：tickets_list、tickets_get、directory_list、tickets_triage、tickets_assign、tickets_request_approval。仅创建输出文件不算完成业务操作。\n交付 output/queue.csv，包含本次处理的所有工单，列 ticket_id,team,assignee,status,due_at；output/summary.json 包含 processed_count,waiting_approval_count,assigned_count,as_of；output/report.md 说明 as_of、policy.json 和服务来源，并用 | waiting_approval | 数量 | 和 | assigned | 数量 | 两行给出计数。记录必须与实际服务状态一致。材料均为合成办公请求，approval 仅模拟内部流程，不发送通知。"""
