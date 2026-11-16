---
name: resolve-scoped-routing-policies
description: "Resolve routing tokens and sharing recipients from current policies matched to each action item's scope and environment, including scoped migrations."
---
## 适用条件
Use when extracting action items and sharing a source under standing routing rules, especially when policies distinguish environments or mark a route as stale.
## 排除条件
Does not supply missing migration targets or authorize sharing beyond the user's request and applicable policy. Archived examples do not establish a current replacement.
## 操作步骤
1. Extract each action item's scope, owner, task and due date from the source; distinguish action owners from observers.
2. Read the applicable routing registry and current migration instructions. Match scope and environment, using an explicit applicable default only when supported.
3. Treat a stale route as unresolved until an authoritative replacement is available. A valid route for staging or another scope is not evidence of a production replacement.
4. Apply migrations only to their stated scope and environment. Preserve valid policies for unaffected scopes; use archived examples only where consistent with current rules.
5. Resolve sharing recipients from the applicable policies and source owners. Share the source within the authorized scope; do not include observers solely because they attended.
6. Render action lines in the user's exact requested format using verified active routes. If a required route remains unresolved, surface the missing policy instead of inventing a token.
## 验证
- Each selected token is supported by a current policy for the action item's scope and environment; stale or differently scoped entries are not used as replacements.
- Unrelated valid routes remain intact, and sharing recipients match the applicable owner or approver rules.
- Output preserves source owners, tasks and due dates and follows the requested line format.
组合依赖 []
