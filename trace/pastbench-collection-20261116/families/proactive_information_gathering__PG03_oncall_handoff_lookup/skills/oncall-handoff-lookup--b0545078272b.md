---
name: oncall-handoff-lookup
description: "Find and confirm the authoritative current oncall handoff before applying narrowly scoped follow-up ticket changes."
---
## 适用条件
Use when a request depends on recovering an incident or service handoff, its owner and maintenance window, and the blocked follow-up tickets from session history.
## 排除条件
Do not infer authority from a higher handoff number, a current-looking title, or service-name overlap alone. This procedure does not authorize parent-incident edits or unrelated notifications; follow the current user's scope.
## 操作步骤
1. Search session history for the latest handoff matching the requested incident or service before accessing or changing tickets.
2. Distinguish the full current handoff from superseded records, incomplete shells, drafts, and recaps. Check incident scope so a canary or related incident is not substituted for the requested incident.
3. When results are ambiguous, re-query the candidate's exact handoff ID and source session. Do not proceed to ticket changes until the authoritative record and required fields are established.
4. Recover the exact handoff ID, incident ID, owner, maintenance window, and blocked follow-up ticket IDs from the confirmed record; do not combine fields from stale or partial records.
5. Inspect only the confirmed blocked follow-up tickets before changing them. For a request to mark waiting on maintenance, set reason_code to waiting_on_maintenance without changing unrelated fields.
6. Update only the blocked follow-up tickets. Do not touch the parent incident or stale ticket set from older handoffs; do not carry one-off instructions from older records into the current task.
## 验证
- The recovered fields come from the same confirmed current handoff for the intended incident, not merely the newest-looking search result.
- Every changed ticket belongs to the confirmed blocked set, and no parent incident or stale ticket was changed.
- For maintenance-wait updates, tool results confirm reason_code is waiting_on_maintenance while unrelated ticket fields remain unchanged.
组合依赖 []
