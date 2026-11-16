---
name: billing-cluster-incident-triage
description: "Correlate open billing tickets by shared cluster failure, apply the current SOP's reporter and timing thresholds, and update only qualifying incident tickets while preserving their open status."
---
## 适用条件
Use when asked to triage open helpdesk tickets under a billing incident SOP that specifies cluster correlation, escalation thresholds, required ticket fields, and exclusions.
## 排除条件
Do not apply incident mutations to below-threshold groups, unrelated clusters, UI bugs, non-cluster performance issues, document or access requests, or routine reports when excluded by the current SOP. Resolution and closure belong to the on-call engineer.
## 操作步骤
1. Read the current task's SOP for thresholds, time window, required fields, exclusions, and output format. Search the KB and retrieve the relevant full article when required; use a brief reference as supporting context rather than a replacement for explicit SOP rules.
2. List all open tickets and read their full descriptions, reporters, timestamps, tags, and status before selecting any tickets for mutation.
3. Group tickets by the same named cluster and supported common failure. Include downstream symptoms such as worker crashes when the description explicitly links them to that cluster outage; keep different clusters separate.
4. For each group, count tickets from distinct reporters within the SOP's time window. Under Billing Incident SOP v2, require at least three within four hours; two affected teams or a narrative claim do not establish three qualifying tickets.
5. Apply the SOP exclusions by the substance of each request. A routine SLA report remains a report request even if its title or description mentions billing incidents or breaches.
6. Update every ticket in a qualifying incident group with the SOP's exact priority, category, and tags. Under v2, use critical, incident-billing, incident-core, and billing-sla-breach. Preserve unrelated fields and existing tags; leave excluded and nonqualifying tickets unchanged.
7. Inspect update results to confirm the requested fields and that ticket status remains open. Do not close tickets; leave resolution to the on-call billing engineer.
8. Return one line per successfully updated ticket in the requested format. Under v2, use TICKET_ID|priority|category|why, with a brief reason grounded in the shared failure and threshold evidence.
## 验证
- Every inspected ticket is accounted for as qualifying, below threshold, or excluded before mutation; tickets from different clusters are not combined to reach a threshold.
- Each escalated group has the required number of distinct reporters and supporting timestamps within the current SOP's window.
- Mutation results confirm the required priority, category, and tags on every qualifying ticket, with open status preserved and unrelated tickets left unchanged.
- Final output contains exactly the successfully updated tickets, with field values matching confirmed results and reasons supported by the ticket evidence.
组合依赖 []
