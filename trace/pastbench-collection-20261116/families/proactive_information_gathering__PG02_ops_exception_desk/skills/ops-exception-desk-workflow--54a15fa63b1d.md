---
name: ops-exception-desk-workflow
description: "Handle an operations exception queue by retrieving the exact temporary authorization, matching its scope, and applying only narrowly approved updates."
---
## 适用条件
Use when handling an operations queue under a previously recorded temporary exception, especially when current ticket details or history summaries omit the exact authorization.
## 排除条件
Does not establish default policy or authorize related queues, partners, lanes, or unlisted tickets. Incomplete summaries alone do not authorize updates. Follow current task restrictions, including any prohibition on closing tickets.
## 操作步骤
1. List the open tickets for the named queue; retain the candidate set for approval and blocked-subset checks.
2. Open relevant ticket details to confirm queue, partner, and scope rather than relying only on titles.
3. Read this desk workflow before acting and honor the current task's constraints.
4. If the exception ID is unknown, search session history using the queue and temporary-exception context. Treat incomplete shells and recaps as discovery hints, not authorization.
5. Search history for the exact candidate exception ID and retrieve the record specifying its queue, partner or lane, approved ticket IDs, and validity period.
6. Match the complete record to ticket scope and check validity against the task's reference time. Do not substitute a different queue, lane, or partner exception or infer approval from partner membership alone.
7. Update only the explicitly approved, in-scope subset. Keep every other ticket blocked under its existing handling; do not generalize the exception as default policy.
8. Change only fields needed to record the authorized approval. Preserve unrelated fields and ticket status; do not close any ticket when prohibited by the current task.
9. Read back the candidate tickets to confirm approved updates, unchanged nonapproved tickets, and preserved statuses before reporting the result in the requested format.
## 验证
- The selected history record supplies an exact exception ID, explicit approved subset, matching scope, and a validity period covering the task reference time.
- Only explicitly approved, in-scope tickets receive approval updates; other same-partner tickets and different-partner tickets remain unchanged.
- Read-back confirms intended approval fields and preservation of unrelated fields and statuses, including no closures when prohibited.
组合依赖 []
