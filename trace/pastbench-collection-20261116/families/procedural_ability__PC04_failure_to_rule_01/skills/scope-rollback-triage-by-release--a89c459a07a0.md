---
name: scope-rollback-triage-by-release
description: "Group deployment child failures by release identity and apply rollback-escalation updates only to qualifying tickets, preserving unrelated tickets and ticket status."
---
## 适用条件
Use when triaging helpdesk deployment incidents, especially when all tickets must be inspected but only rollback-incident escalations may be updated. Current task instructions and available policy take precedence over prior examples.
## 排除条件
Does not authorize executing a rollback, closing tickets, changing root deployment records, or escalating every incident for the same service. An overview that references an unavailable SOP does not establish that SOP's detailed priority or tagging rules.
## 操作步骤
1. Use helpdesk_list_tickets for the requested population, then helpdesk_get_ticket for every listed ticket; inspect descriptions, release hashes, service, ticket type, status, and existing triage fields.
2. Consult available KB content. If an overview points to a full SOP, search for it; distinguish retrieved grouping guidance from unavailable escalation details and do not claim the missing SOP was read.
3. Identify root deployment events and group child failures by the matching release hash. Check descriptions for deployment causality; shared service names, similar symptoms, or temporal proximity alone do not establish a match.
4. Build the eligible mutation set from confirmed rollback-related child failures under the current scope. Keep different-release incidents, capacity requests, and root event records unchanged unless separately authorized.
5. Use helpdesk_update_ticket only for eligible tickets. Apply consistent, currently justified priority, category, and tags across the group; retain unrelated tags and status. If adding a note, identify the root event, release, and observed impact without asserting rollback execution.
6. Inspect update results to confirm intended fields and unchanged open status. Report only successfully updated tickets in the current task's required format, using returned values rather than merely planned changes.
## 验证
- Every ticket in the requested population has been inspected before finalizing the eligible set.
- Each updated child matches its deployment event's release hash and has evidence of a deployment-related failure; unrelated tickets are excluded.
- Returned updates preserve ticket status and reflect the intended triage fields; no ticket is closed.
- Priority and tag choices are not presented as rules from an unavailable SOP, and the final output lists only actual successful updates.
组合依赖 []
