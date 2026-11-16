---
name: source-grounded-migration-ticket-triage
description: "Inspect migration tickets and correlate region, schema transition, and shard configuration against an authoritative rule before applying narrowly scoped escalation updates."
---
## 适用条件
Use when helpdesk migration triage depends on multiple ticket attributes, especially when KB search yields an overview that references a separate correlation SOP.
## 排除条件
Does not supply migration-risk thresholds or infer them from ticket examples, urgency wording, monitoring claims, or successful updates. Current task instructions govern qualification, encoding, and mutation scope.
## 操作步骤
1. Separate the task's eligibility criteria, escalation field encoding, inspection scope, and allowed mutation scope. Required labels alone do not define which tickets qualify.
2. Search the KB and read the relevant article in full. If it references a separate correlation SOP, search for that source rather than treating the overview as the complete rule.
3. Use helpdesk_list_tickets for the requested scope, then helpdesk_get_ticket for every listed ticket, including routine-looking or apparently urgent tickets.
4. Record each ticket's region, schema source and target versions, shard count, and relevant description. Preserve missing values as unknown; do not infer them from titles alone.
5. Evaluate the attributes together using explicit task criteria or a retrieved authoritative rule. Single-indicator matches are insufficient when the source requires correlation.
6. If precise criteria remain unavailable, distinguish unresolved eligibility from a negative result. Report the rule gap and seek clarification rather than inventing version, region, or shard thresholds.
7. Use helpdesk_update_ticket only for established qualifying escalations within the allowed scope. Apply the requested encoding and leave other tickets and unrelated fields unchanged.
8. Check update responses against intended changes. Produce the requested output from inspected and confirmed state, without presenting tool success as validation of the risk decision.
## 验证
- Every ticket in the requested scope has a complete inspected record, with missing correlation attributes explicitly recognized.
- Each escalation decision is traceable to an explicit qualification rule, not merely a title, one matching attribute, or the desired label format.
- Nonqualifying and unresolved tickets receive no escalation mutation when updates are limited to qualifying tickets.
- Confirmed update fields match the requested encoding, and unresolved rule gaps are reported without claiming those tickets are safe.
组合依赖 []
