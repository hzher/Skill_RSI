---
name: billing-refund-sop
description: "Distinguish routine refund requests from refund-related billing cluster failures."
---
## 适用条件
Use when deciding whether a refund-related ticket is a routine request or a possible shared billing-cluster incident.
## 排除条件
Does not define refund approval or payment execution procedures. A refund-related title alone does not establish or exclude a cluster incident.
## 操作步骤
1. Read the ticket's detailed description to distinguish a routine refund request or reconciliation report from a service failure affecting refund processing.
2. Treat routine refund requests as non-incidents. Under an incident-only triage task, leave routine requests unchanged.
3. For refund processing failures linked to a shared billing cluster, apply billing-incident-sop to establish the reporter and time-window threshold and perform any required escalation.
## 验证
- Confirm classification follows the reported failure or request, rather than the presence of the word refund.
- Confirm any refund-related cluster escalation satisfies the current billing-incident-sop; routine refund requests remain excluded from incident classification.
组合依赖 ["billing-incident-sop"]
