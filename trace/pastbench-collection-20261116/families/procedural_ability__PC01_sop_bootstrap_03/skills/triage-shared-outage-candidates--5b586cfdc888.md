---
name: triage-shared-outage-candidates
description: "Correlate active outage reports using full ticket details, exclude incidental infrastructure mentions, and distinguish suspected shared incidents from policy-qualified escalation."
---
## 适用条件
Use when reviewing helpdesk tickets for shared outages and only procedure-qualified tickets may be changed, especially when KB search returns an overview instead of the governing SOP.
## 排除条件
Does not define outage thresholds, priority levels, categories, or tags. Does not authorize changes outside the current task's mutation scope or replace a governing SOP with a correlation heuristic.
## 操作步骤
1. Read the current task's mutation scope, required tools, closure restrictions, and output contract before processing tickets.
2. Search the KB for outage triage guidance and read the relevant article. If it refers to a full SOP, search for that procedure rather than treating the overview as complete classification guidance.
3. Use helpdesk_list_tickets to establish the review set and helpdesk_get_ticket to inspect each ticket's full description and existing fields.
4. Compare active failure reports by infrastructure zone, onset time, affected services, and customer impact. Treat correlated failures as shared-outage candidates, not proof of a common root cause.
5. Exclude feature requests and planned maintenance without an active failure merely sharing an infrastructure term. Do not reclassify these tickets when mutation is restricted to outage escalation.
6. Evaluate candidates against the available governing procedure. Do not invent escalation thresholds, priorities, categories, or tags from an overview that only says a shared outage may exist.
7. When qualification and required fields are established, use helpdesk_update_ticket only for eligible tickets within scope and check its response. Preserve unrelated fields and obey any prohibition on closing tickets.
8. If required policy is unavailable, report candidates and the missing policy, request it, and explicitly disclose unperformed updates. Follow the requested ticket-output format without claiming that analysis fulfilled a mandatory update requirement.
## 验证
- Every reviewed ticket has full-detail evidence; a matching zone name alone is not treated as an active incident.
- Shared-outage candidates are distinguished from confirmed policy-qualified escalations, with no unsupported priority or category assignments.
- Any mutations have tool confirmation, affect only eligible tickets, and respect closure restrictions; if blocked, the report clearly states that required updates remain incomplete.
组合依赖 []
