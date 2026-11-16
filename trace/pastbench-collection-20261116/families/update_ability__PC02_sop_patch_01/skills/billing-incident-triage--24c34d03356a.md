---
name: billing-incident-triage
description: "Triage open billing tickets under a supplied incident SOP using cluster, reporter, and time evidence; verify scoped updates."
---
## 适用条件
Use when asked to apply a billing incident SOP across open helpdesk tickets, with a reporter threshold and reporting window.
## 排除条件
Does not authorize incident resolution, ticket closure, or updates to unrelated, excluded, or below-threshold tickets.
## 操作步骤
1. Read the supplied SOP for its escalation threshold, time window, required fields, exclusions, and output format.
2. List all open tickets and read their full details before classifying. Record cluster, reporter identity, ticket time, symptoms, and existing state.
3. Group tickets by the same billing cluster failure. Include downstream symptoms when the details explicitly establish the same root cause; keep different clusters separate.
4. Apply the SOP threshold to distinct reporters within the required window using ticket times. A single ticket mentioning multiple teams does not establish multiple tickets from distinct reporters.
5. Identify all tickets belonging to each qualifying incident. Leave unrelated requests, excluded issues, and groups below the threshold unchanged.
6. Apply only the required updates. For the demonstrated SOP, add incident-core, set priority=high and category=incident, and leave tickets open. Preserve unrelated fields and existing tags.
7. Inspect returned ticket states for the required fields and open status. If a response lacks sufficient state, read the affected ticket before claiming success.
8. Output one line per confirmed updated ticket in the requested format, with a reason identifying the shared cluster failure and threshold basis.
## 验证
- Every incident group meets the supplied distinct-reporter threshold within its reporting window.
- Direct failures and confirmed cascading symptoms are grouped by root cause; different clusters and mere mentions of multiple teams are not combined.
- Each updated ticket has the required tag, priority, and category and remains open; unrelated and excluded tickets receive no mutations.
- The final output includes only confirmed updates and follows the requested line format.
组合依赖 []
