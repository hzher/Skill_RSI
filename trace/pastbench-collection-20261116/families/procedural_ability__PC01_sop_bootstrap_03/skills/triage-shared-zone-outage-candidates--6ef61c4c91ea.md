---
name: triage-shared-zone-outage-candidates
description: "Identify potential shared infrastructure outages across helpdesk tickets by correlating active failures, infrastructure dependencies and onset times, while separating unrelated work and locating the governing escalation procedure."
---
## 适用条件
Use when reviewing multiple helpdesk tickets for zone-related outages or infrastructure failures, especially when incident reports and routine requests mention the same zone.
## 排除条件
Does not define escalation thresholds, priorities or tag names. Candidate correlation alone does not authorize escalation; qualification and permitted updates must follow the current task and governing procedure.
## 操作步骤
1. Use helpdesk_list_tickets to enumerate tickets, then helpdesk_get_ticket to inspect each ticket's full description and current fields; do not classify solely from titles.
2. Extract the affected zone, service or dependency, failure symptoms, onset time and reported impact for each active incident.
3. Group potential shared outages using the common zone together with compatible timing and concrete infrastructure failures, such as storage I/O errors or connection failures.
4. Separate feature requests, planned improvements and routine maintenance from active outage candidates even when they reference the same zone; honor any instruction to leave those tickets unchanged.
5. Search for the governing shared-outage procedure and read its full content. If an overview refers to a separate SOP, seek that SOP rather than deriving classification or tagging rules from the overview.
6. Record candidate incidents, supporting evidence and excluded requests. If governing criteria remain unavailable, report the missing procedure and pending escalation decision without inventing tags or claiming updates occurred.
## 验证
- Every listed ticket has been inspected in full and accounted for in the assessment.
- Each suspected shared outage has failure evidence beyond a repeated zone name; timing and dependency evidence are compared where available.
- Any escalation classification or proposed mutation is supported by the current governing procedure; unavailable criteria and unperformed updates are stated explicitly.
组合依赖 []
