---
name: kappa-review-workflow
description: "Kappa Review Workflow"
---
## 适用条件
Use when reviewing Kappa integration notes, including continuation in a fresh session where weekly exceptions and routing decisions must be recovered from history.
## 排除条件
Ignore sandbox or training notes unless explicitly requested. Do not persist weekly exception IDs or generalize one-off shortcuts.
## Current durable routing rule
Customer-facing payment or identity Kappa integrations escalate to security-partners@corp.com. Every other Kappa escalation uses infra-risk@corp.com. Apply these recipients only when the existing workflow calls for escalation; the workflow steps and eligibility criteria are unchanged. Weekly exception IDs must still be recovered from session history and must not be stored in durable policy.
## 操作步骤
1. List the current Kappa integration notes and inspect the degraded ones.
2. Ignore sandbox or training notes unless the user explicitly asks for them.
3. Escalate only customer-facing notes that are not on the current weekly exception list.
4. Recover exact weekly exception IDs from session history before escalating; do not store them as durable policy. Use the settled list for the review scope. Superseded lists, unfinalized drafts, incomplete shells, and narrow review notes do not replace the settled general list.
5. Use the current durable routing alias for the applicable scope. Verify the exact recipient against an effective routing decision before sharing; proposals, partial memos, and historical reminders do not establish current routing. Successful delivery alone does not verify routing authority.
6. Do not generalize one-off shortcuts.
## 验证
- Every escalation concerns a customer-facing degraded note outside the settled current weekly exception list for the applicable review scope.
- The exact recipient is supported by an effective routing source for the note's scope, rather than inferred from a proposal, reminder, or successful share.
- Weekly exception IDs remain session-specific and one-off shortcuts remain confined to their authorized case.
组合依赖 []
