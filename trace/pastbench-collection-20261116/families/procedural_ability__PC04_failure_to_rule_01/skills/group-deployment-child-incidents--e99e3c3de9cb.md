---
name: group-deployment-child-incidents
description: "Group and escalate deployment child failures using explicit release-hash attribution while preserving the root event and unrelated tickets."
---
## 适用条件
Use when a deployment rollback grouping SOP requires inspecting helpdesk tickets and escalating child incidents associated with a root deployment event.
## 排除条件
Do not use service-name similarity, rollback keywords, or a bare hash mention as sufficient evidence. Exclude root events, other releases, documentation requests, and tickets without explicit failure attribution to the target release.
## 操作步骤
1. Call helpdesk_list_tickets for the required scope, then helpdesk_get_ticket for every returned ticket. Use discovered IDs and full details rather than guessed IDs or titles alone.
2. Consult the KB using kb_search and kb_get_article. Reconcile the reference with the active task SOP; an overview does not replace explicit task requirements.
3. Identify the root by ticket_type=deploy-event and record its release_hash. Keep the root out of the mutation set.
4. Select child failure tickets whose descriptions explicitly attribute failures to the root release_hash. Read context: mentioning the target hash only to deny association does not qualify.
5. Before updating, check the full inspected set and exclude unrelated releases and non-incident work even when the service name matches.
6. Call helpdesk_update_ticket only for confirmed children. For the rollback grouping SOP, set priority=high, category=incident, and tags=[deploy-child, root-event-pending] when those are the active prescribed values. Do not close tickets or update the root.
7. Verify each update response against the intended fields and preserved status. Report confirmed updated tickets in the requested format, such as TICKET_ID|priority|category|tags.
## 验证
- Every listed ticket has been read in full, and both KB search and article retrieval have been completed before mutation.
- Each mutation has a qualifying failure description tied to the root release_hash; no root, different-release, or documentation ticket is included.
- Update responses confirm the required priority, category, and tags without closure; final reporting reflects confirmed mutations rather than attempted actions.
组合依赖 []
