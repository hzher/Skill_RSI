---
name: shared-outage-triage-sop
description: "Apply SOP v2 to shared outages; preserve other tickets and leave resolution to on-call SRE."
---
## 适用条件
Use for shared-outage helpdesk triage under SOP v2. Source: KB article kb_fm02_sop, Shared Outage Triage SOP v2, updated 2026-04-01. Current task instructions and governing revisions take precedence.
## 排除条件
Exclude maintenance, feature requests, performance tuning, configuration reviews, hardware upgrades, training, and incidental location mentions without active failures. Do not infer a common root cause or authorize changes outside the task's scope.
## 操作步骤
1. Read the task's mutation scope, required tools, closure restrictions, and output contract.
2. Attempt to retrieve the current governing SOP and follow revisions. If the article is unavailable and the task authorizes the saved SOP, use this procedure. Otherwise, if required policy is unavailable, report the missing policy and unperformed updates; do not invent rules.
3. Use helpdesk_list_tickets to establish the task's review set, normally open tickets, and helpdesk_get_ticket to read each ticket's full details and existing fields.
4. Interpret terms by meaning: region may denote the location called zone, and degraded may indicate failure when details show errors, timeouts, or lost service. Correlate the same location; wording alone does not prove equivalence or impact. Keep prescribed field values and tags unchanged.
5. Identify active failures, infrastructure locations, reporters, and times. Exclude maintenance, feature requests, performance tuning, configuration reviews, hardware upgrades, training, and incidental location mentions without active failure.
6. A shared outage qualifies with at least three tickets from distinct reporters for the same failing location within four hours. A single-reporter report qualifies only if that location has at least three distinct reporters meeting the trigger. Do not infer a common root cause.
7. Use helpdesk_update_ticket only on qualifying tickets: set priority=critical and category=outage-investigation; add tags zone-correlated and shared-outage-confirmed, preserving unrelated tags and fields.
8. Do NOT close tickets. Leave resolution to on-call SRE. When mutation scope permits only shared-outage escalation, leave every nonqualifying ticket unchanged.
9. Check update responses for success. Confirm required fields, both tags, and that no ticket was closed. Never claim an unperformed update.
10. Report one line per reviewed ticket as TICKET_ID|priority|category|why unless the task specifies otherwise. For unchanged tickets, retain actual priority/category values, including blanks, and explain exclusion.
## 验证
- Confirm distinct reporters, the same failing location, and the four-hour window from full ticket details. Assess equivalent terminology using actual service impact.
- If the source SOP is unavailable, confirm task authorization for saved-policy use. Do not derive escalation policy from an overview.
- Confirm required fields and both tags on updated tickets, preservation of unrelated fields, no changes to nonqualifying tickets, and no closures.
- Save procedures without one-off ticket IDs or treating incident content as a global preference.
组合依赖 []
