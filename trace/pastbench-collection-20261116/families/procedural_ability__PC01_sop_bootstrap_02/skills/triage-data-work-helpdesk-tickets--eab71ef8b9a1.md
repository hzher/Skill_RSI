---
name: triage-data-work-helpdesk-tickets
description: "Triage open helpdesk tickets by the outstanding data-quality, preprocessing, or analysis work; apply supported updates without changing unrelated infrastructure requests."
---
## 适用条件
Use when asked to inspect helpdesk tickets for data processing, data quality, or analysis needs and to update tickets covered by that scope.
## 排除条件
Does not perform the underlying data remediation or infrastructure provisioning. Do not apply fixed priority or escalation mappings from these examples; current instructions and available SOP rules take precedence.
## 操作步骤
1. List open tickets with helpdesk_list_tickets, then inspect every listed ticket with helpdesk_get_ticket; use full descriptions rather than titles or departments alone.
2. Retrieve relevant knowledge-base guidance. Distinguish a pipeline overview from detailed classification or escalation policy; if the detailed SOP is unavailable, do not claim its rules were verified.
3. Identify the outstanding work: invalid values needing cleaning or filtering indicate data-quality work; missing normalization indicates preprocessing; aggregation and reporting on already-clean data indicate analysis.
4. Respect completed stages: do not prescribe cleaning again merely because a ticket concerns data. Explain the remaining work and any prerequisite needed before analysis can proceed.
5. Separate unrelated or explicitly excluded requests, such as storage-capacity provisioning, from in-scope data work. Inspect them but leave them unchanged unless current instructions authorize that classification or escalation.
6. For each in-scope ticket, use helpdesk_update_ticket to set the supported category and priority and record a concise rationale and next action. Base priority on available policy and impact, not category alone.
7. Check update results for the intended fields and success. Report ticket ID, priority, category, and rationale in the current requested format, distinguishing confirmed updates from failures or unchanged tickets.
## 验证
- Every ticket returned by the open-ticket listing has been inspected in full.
- Each mutation is within the requested classification or escalation scope; unrelated and excluded tickets retain their original fields.
- Rationales distinguish data-quality defects, preprocessing prerequisites, and downstream analysis without repeating completed work.
- Reported updates match successful tool results; unavailable detailed SOP rules are not presented as verified policy.
组合依赖 []
