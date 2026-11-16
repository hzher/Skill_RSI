---
name: ops-exception-desk-workflow
description: "Handle an operations exception queue by retrieving the exact temporary authorization, matching its scope, and applying only narrowly approved updates."
---
## 适用条件
Use when handling an operations queue under a previously recorded temporary exception, especially when current ticket details or history summaries omit the exact authorization.
## Retrieval policy
Open the desk workflow skill first. Search authorized session history for the exact temporary exception, retrieve its complete authorization, and verify its scope and validity before updating tickets. Do not turn one-off exceptions into default policy.
## 排除条件
Does not establish default policy or authorize related queues, partners, lanes, or unlisted tickets. Incomplete summaries alone do not authorize updates. Follow current task restrictions, including any prohibition on closing tickets.
## 操作步骤
1. Open and read this desk workflow skill first, before listing or updating tickets, and honor the current task's constraints.
2. List the open tickets for the named queue; retain the candidate set for approval and blocked-subset checks.
3. Open relevant ticket details to confirm queue, partner, and scope rather than relying only on titles.
4. If the exception identifier is unknown, search authorized session history using the queue and temporary-exception context. Treat incomplete shells and recaps as discovery hints, not authorization. Use only the provided session_search tool to retrieve authorized history.
5. Search session history for the exact temporary exception using its candidate identifier and retrieve the record specifying its queue, partner or lane, explicitly approved tickets, and validity period.
6. Before updating any ticket, match the complete authorization record to ticket scope and check validity against the task's reference time. Do not substitute a different queue, lane, or partner exception or infer approval from partner membership alone. If authorization is incomplete or scope cannot be verified, do not apply the approval update.
7. Update only the explicitly approved, in-scope subset. Keep every other ticket blocked under its existing handling. Do not turn one-off exceptions into default policy.
8. Change only fields needed to record the authorized approval. Preserve unrelated fields and ticket status; do not close any ticket when prohibited by the current task.
9. Read back the candidate tickets to confirm approved updates, unchanged nonapproved tickets, and preserved statuses before reporting the result in the requested format.
## 验证
- The desk workflow skill was opened first.
- The selected session-history record supplies the exact temporary exception, explicit approved subset, matching scope, and a validity period covering the task reference time.
- Scope and validity were verified before ticket updates.
- Only explicitly approved, in-scope tickets receive approval updates; other same-partner tickets and different-partner tickets remain unchanged.
- Read-back confirms intended approval fields and preservation of unrelated fields and statuses, including no closures when prohibited.
- One-off exceptions remain limited authorizations and are never promoted to default policy.
组合依赖 []
