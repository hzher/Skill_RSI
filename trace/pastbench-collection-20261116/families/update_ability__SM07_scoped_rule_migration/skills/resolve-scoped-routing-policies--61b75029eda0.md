---
name: resolve-scoped-routing-policies
description: "Resolve routing tokens and sharing recipients from current policies matched to each action item's scope and environment, including scoped migrations and priority-qualified sharing rules."
---
## 适用条件
Use when extracting action items and sharing a source under standing routing rules, especially when policies distinguish environments, priorities, or mark a route as stale.
## 排除条件
Does not supply missing migration targets or authorize sharing beyond the user's request and applicable policy. Archived examples, rendered answers, and sharing records do not establish a current replacement.
## 操作步骤
1. Extract each action item's scope, owner, task and due date from the source; distinguish action owners from observers.
2. Read the applicable routing registry and current migration instructions. Match scope, environment, and any priority-qualified sharing rule.
3. Treat a stale route as unresolved until an authoritative replacement is available. A valid route for staging or another scope is not evidence of a production replacement.
4. Apply migrations only to their stated scope and environment. Preserve valid policies for unaffected scopes; use archived examples only where consistent with current rules.
5. Do not infer an active token from a prior rendered answer or a sharing record; those artifacts can confirm an outcome but cannot replace current routing-policy evidence.
6. For mixed Security and Ops notes, include all Security action items in the rendered output, but share the source only with High-priority Security owners when that current policy applies; share Ops items with all authorized Ops owners.
7. Resolve sharing recipients from applicable policies and source owners. Do not include observers solely because they attended.
8. Render action lines in the user's exact requested format using verified active routes. If a required route remains unresolved, surface the missing policy instead of inventing a token.
## 验证
- Each selected token is supported by a current policy for the action item's scope and environment; stale or differently scoped entries, prior answers, and sharing records are not used as replacements.
- Priority-qualified sharing rules are enforced: all required Security lines remain in output, while source sharing is limited to authorized High-priority Security owners; Ops recipients follow the unchanged Ops rule.
- Unrelated valid routes remain intact, observers are excluded unless authorized, and sharing recipients match applicable owner or approver rules.
- Output preserves source owners, tasks and due dates and follows the requested line format.
组合依赖 []
