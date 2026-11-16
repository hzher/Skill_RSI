---
name: risk-review-archive
description: "Apply the durable risk-review default while keeping archived temporary exceptions distinct from current routing policy."
---
## 适用条件
Use when reviewing risk-review notes, formatting their action items, sharing the source note with owners, or reconciling stale temporary exceptions in a rule registry.
## 排除条件
Do not apply audit-note formats or audit-only sharing exceptions to risk reviews. Historical one-week exceptions are not durable policy; observers are not recipients merely because they attended.
## 操作步骤
1. Read the source note and relevant rule registry; identify action item owners, tasks, and due dates separately from attendees or observers.
2. Output each risk-review action item as <owner> | RISK-CANON | task | due_date.
3. Check temporary exceptions for scope and expiry. Archived one-week sharing omissions do not establish durable routing policy; the stale risk-review sharing exception in the observed registry was explicitly expired.
4. Apply the standing default of sharing the source note with all action item owners when no applicable active exception remains.
5. When reconciling an explicitly expired stale exception, update that registry clause to mark it inactive while preserving the durable default and unrelated scoped rules.
6. Report the action items and confirmed sharing result; disclose if the available note does not match the requested date.
## 验证
- Each action item retains its source owner, task, and due date and uses RISK-CANON.
- Sharing recipients match action item owners under the applicable current policy; observer attendance alone does not add a recipient.
- Any expiry update is supported explicitly and preserves unrelated exceptions and rules.
组合依赖 []
