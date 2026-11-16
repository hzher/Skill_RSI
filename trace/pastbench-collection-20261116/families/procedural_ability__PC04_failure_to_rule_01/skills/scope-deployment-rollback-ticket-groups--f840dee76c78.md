---
name: scope-deployment-rollback-ticket-groups
description: "Group deployment-related child failures by release hash and restrict rollback-triage updates to qualifying tickets, keeping root events and unrelated work outside the mutation set."
---
## 适用条件
Use when triaging helpdesk tickets into deployment rollback groups, especially when one service has multiple releases or a mixture of deployment events, child failures, and unrelated requests.
## 排除条件
Does not establish universal priority, category, or tag values, authorize closing tickets, or authorize updating every ticket sharing a service name. Follow current task scope and available policy for those decisions.
## 操作步骤
1. Use helpdesk_list_tickets to enumerate the requested scope, then helpdesk_get_ticket to inspect each ticket's full description, release_hash, service, ticket_type, and status before selecting updates.
2. Consult the available deployment-triage KB. Distinguish explicit grouping guidance from priority, category, and tagging rules that an overview may omit; do not treat observed update values as a universal SOP.
3. Identify root deployment events and map child failures to the corresponding release hash using structured fields and causal details. Group related child failures together rather than triaging each in isolation.
4. Keep different release hashes in separate groups even when service names match. Exclude unrelated capacity or planning requests; do not automatically include root-event records in a child-failure mutation set.
5. Apply the current task's rollback-escalation eligibility criteria to each group and form an explicit update set. Membership in the ticket inventory alone does not authorize mutation.
6. Use helpdesk_update_ticket only for eligible tickets with the applicable priority, category, and tags. Leave nonqualifying tickets and unrelated fields unchanged; when closure is forbidden, do not change status to closed.
7. Check update responses for the intended fields and retained status. Report only confirmed updates in the current task's required format.
## 验证
- Every selected child failure has evidence linking it to its deployment group; a common service name alone is insufficient.
- Different-release incidents, unrelated requests, and root-event records remain untouched unless current instructions independently authorize their update.
- Updated fields match the applicable instructions or policy, and all no-closure and output-format requirements are preserved.
组合依赖 []
