---
name: billing-incident-sop
description: "Billing Incident SOP for identifying shared billing-cluster incidents and escalating covered tickets without closing them."
---
## 适用条件
Use for open tickets describing a billing or payment cluster failure when at least two open tickets share that same cluster or confirmed root cause.
## 排除条件
Do not classify unrelated clusters, such as promo-cluster, or routine reporting and other non-incident requests as billing incidents; do not close tickets.
## 操作步骤
1. Inspect all open tickets and their detailed descriptions.
2. Group tickets by billing or payment cluster and confirmed shared root cause.
3. Escalate a group when two or more open tickets share the same billing cluster or root cause.
4. Set each covered ticket priority to high and category to incident.
5. Add the incident-core tag and an internal note explaining the shared-cluster basis.
6. Leave every covered ticket open for the on-call billing engineer.
## 验证
- Confirm at least two open tickets support the same billing cluster or root cause.
- Confirm every covered ticket has priority high, category incident, and tag incident-core.
- Confirm covered tickets remain open and unrelated or routine tickets are unchanged.
组合依赖 []
