---
name: triage-rollback-child-tickets
description: "Group deployment failure children by release hash, evaluate their combined incident impact, and apply scoped escalation updates while preserving unrelated tickets and required open status."
---
## 适用条件
Use when processing deployment incident tickets, especially when inspection covers all open tickets but mutations are limited to qualifying rollback-incident child escalations. Follow the current task's scope and available deployment SOP.
## 排除条件
Does not authorize executing a rollback, closing tickets, or updating root events and unrelated requests outside scope. A shared service name or rollback keyword alone does not establish a shared deployment event.
## 操作步骤
1. Read the current task's mutation limits and deployment KB guidance. If an overview references a fuller SOP, search for it; do not present inferred priority or tagging conventions as verified SOP requirements.
2. Use helpdesk_list_tickets to enumerate the requested ticket set, then helpdesk_get_ticket to inspect each ticket's description, release hash, service, type, and status rather than classifying by title alone.
3. Identify root deployment events and map child failures to the matching release hash. Keep different release hashes separate even for the same service; distinguish routine documentation from deployment failures.
4. Assess the children together: correlate deployment timing, reported symptoms, affected users or endpoints, and confirmation of regression. Use combined impact to decide whether the group qualifies for rollback escalation.
5. Select only tickets authorized by the current mutation scope. A separate-release rollback request must not be added to the current group merely because it mentions rollback; assess any separate group only if scope permits.
6. For qualifying children, choose priority, category, and grouping tags using applicable guidance. Observed outage updates used urgent, deployment-incident, and tags rollback-incident, release:<hash>, parent:<root-ticket>; treat these as an example, not a universal SOP.
7. Use helpdesk_update_ticket for each eligible child. Where supported, add an internal note identifying its root event, related children, evidence of impact, and need for rollback evaluation. Do not execute rollback or close tickets without authorization.
8. Check update responses for the intended fields and required open status. Leave nonqualifying tickets unchanged and report only confirmed updates in the format required by the current task.
## 验证
- Every grouped child has evidence tying it to the same release hash and matching root event; shared service names alone are insufficient.
- The mutation set contains only qualifying tickets within the current task's scope; unrelated releases, documentation, and root events remain unchanged when excluded.
- Priority reflects documented combined impact, and tags identify the intended group without claiming unavailable SOP authority.
- Tool responses confirm the requested updates and no unauthorized closure; the final report contains only confirmed updated tickets.
组合依赖 []
